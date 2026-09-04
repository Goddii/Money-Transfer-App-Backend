"""Safaricom Daraja (M-Pesa) integration.

Only the approved MVP flow is implemented: an STK Push deposit that credits the
user's Vyloc wallet after Safaricom confirms the payment.

Credentials are read from configuration/environment variables, are never
hardcoded, and are never logged or returned in an API response.
"""

import base64
import threading
import time
from datetime import datetime

import requests
from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.mpesa_transaction import MpesaTransaction, MpesaTransactionStatus
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService
from app.utils.errors import ApiError, ErrorCode
from app.utils.helpers import (
    generate_account_reference,
    mask_phone_number,
    to_money,
    truncate,
)

import random
import threading

TOKEN_PATH = "/oauth/v1/generate?grant_type=client_credentials"
STK_PUSH_PATH = "/mpesa/stkpush/v1/processrequest"
STK_QUERY_PATH = "/mpesa/stkpushquery/v1/query"

REQUIRED_CONFIG_KEYS = (
    "DARAJA_CONSUMER_KEY",
    "DARAJA_CONSUMER_SECRET",
    "DARAJA_SHORTCODE",
    "DARAJA_PASSKEY",
    "DARAJA_CALLBACK_URL",
)

RESULT_DESC_MAX_LENGTH = 255

# Only these Daraja environments are valid. Anything else is normalised to
# ``sandbox`` (the safe default) at startup so a typo can never silently select
# a production endpoint while sandbox credentials are used (or vice versa).
ALLOWED_DARAJA_ENVS = ("sandbox", "production")
SANDBOX_HOST = "sandbox.safaricom.co.ke"
PRODUCTION_HOST = "api.safaricom.co.ke"


def _url_path(url):
    """Return just the path component of a URL.

    The full URL (which may embed a host) is never logged in full; only the
    path is, so this also guards against accidentally logging any query string
    or credential that might ever be appended upstream.
    """
    try:
        from urllib.parse import urlparse

        return urlparse(url).path or url
    except Exception:
        return url


class DarajaHttpError(ApiError):
    """Raised for classified HTTP errors from Daraja.

    Carries the upstream HTTP status so callers can decide backoff behaviour
    without inspecting logs. This is an internal refinement of ``ApiError``
    and is never leaked to end-users.
    """

    def __init__(self, message, http_status=None, status_code=502, error_code=ErrorCode.MPESA_REQUEST_FAILED):
        super().__init__(message, status_code=status_code, error_code=error_code)
        self.http_status = http_status


class DarajaThrottled(DarajaHttpError):
    """Raised when the shared Daraja rate limiter refuses a permit.

    This is a controlled, application-wide rejection (not an upstream error):
    the caller must treat it like an upstream-unavailable condition — do NOT
    credit, do NOT mark the deposit failed, and do NOT increment the
    per-transaction reconciliation attempt counter.
    """

    def __init__(self, retry_after=None, reason=None):
        super().__init__(
            "M-Pesa is rate limited. Please try again shortly.",
            http_status=429,
        )
        self.retry_after = retry_after
        self.reason = reason


class DarajaUpstreamCooldown(DarajaHttpError):
    """Raised when Daraja is in a global upstream cooldown (403/429/5xx).

    Like :class:`DarajaThrottled`, this must NOT be charged as a per-transaction
    reconciliation attempt and must NOT change a deposit's financial state.
    """

    def __init__(self, http_status=429, retry_after=None, reason=None):
        super().__init__(
            "M-Pesa upstream is temporarily unavailable.",
            http_status=http_status,
        )
        self.retry_after = retry_after
        self.reason = reason


class DarajaTransactionUnknown(DarajaHttpError):
    """Raised when Daraja's STK Query says one specific checkout request is
    unknown/expired.

    Safaricom returns this as an HTTP 500 whose JSON body carries a
    per-transaction ``errorCode`` (the ``500.001.xxxx`` family), e.g.::

        {"requestId": "ws_CO_...",
         "errorCode": "500.001.1001",
         "errorMessage": "The transaction does not exist"}

    This is a TERMINAL result for that transaction alone — the
    ``CheckoutRequestID`` is not (or is no longer) known to Safaricom — and is
    NOT a sign that the Daraja API is degraded. It must therefore never trip the
    shared global upstream cooldown. The reconciliation layer stops polling the
    transaction and routes it to manual review.
    """

    def __init__(self, error_code=None, error_message=None):
        super().__init__(
            "M-Pesa reports this transaction no longer exists.",
            http_status=500,
        )
        self.error_code = error_code
        self.error_message = error_message


# --- Daraja STK Query per-transaction error classification --------------------
#
# The STK Push *Query* endpoint reports the state of a single checkout request
# by returning an HTTP 500 with a JSON body of the form
# ``{"errorCode": "500.001.xxxx", "errorMessage": "..."}``. These codes describe
# THAT transaction, never the health of the Daraja API, so they must be handled
# per-transaction and must never trigger the global "daraja_5xx" cooldown that
# gates every other in-flight deposit (and, previously, brand-new STK pushes).
#
# Reference: Safaricom Daraja "STK Push Query" error responses. ``500.001.1001``
# is the code observed in production ("The transaction does not exist" when the
# checkout id is unknown/expired; "...is being processed" while the customer has
# not yet acted on the STK prompt). The whole ``500.001.`` family is treated as
# per-transaction; membership is also configurable via
# ``DARAJA_QUERY_TRANSACTION_ERROR_CODES``.
DARAJA_QUERY_TRANSACTION_ERROR_CODES = frozenset({"500.001.1001"})
DARAJA_QUERY_TRANSACTION_ERROR_PREFIX = "500.001."

# errorMessage substrings (matched case-insensitively) that mean the checkout
# request id is unknown/expired — a TERMINAL result for that transaction.
DARAJA_TRANSACTION_UNKNOWN_MESSAGE_MARKERS = (
    "does not exist",
    "doesn't exist",
    "no transaction",
    "not found",
    "invalid transaction",
    "invalid checkout",
    "unknown checkout",
)

# errorMessage substrings that mean the STK prompt for this checkout is still in
# flight — a TRANSIENT result: keep the deposit recoverable and retry later.
DARAJA_TRANSACTION_PROCESSING_MESSAGE_MARKERS = (
    "being processed",
    "is processing",
    "under processing",
    "already in process",
    "still processing",
)

# Cooldown reasons whose blast radius is limited to background reconciliation
# polling. A 5xx seen while polling a stale transaction must not gate a
# different user's brand-new STK Push initiation; 429 (shared Daraja quota) and
# 403 (WAF) are genuinely application-wide and keep gating everything.
RECONCILIATION_SCOPED_COOLDOWN_REASONS = frozenset({"daraja_5xx"})

# Daraja call domains. ``initiation`` covers the user-facing STK Push (and the
# OAuth token fetched for it); ``reconciliation`` covers every background/query
# path (STK Query, callback-triggered query, sweeper, admin).
DARAJA_DOMAIN_INITIATION = "initiation"
DARAJA_DOMAIN_RECONCILIATION = "reconciliation"


# ---------------------------------------------------------------------------
# Token cache (keyed, thread-safe, test-isolatable)
# ---------------------------------------------------------------------------
#
# Tokens are cached per (DARAJA_BASE_URL, DARAJA_CONSUMER_KEY) so that sandbox
# and production credentials can never share a token, and so that an app
# instance / test can reset the cache without leaking state into another. The
# cache is in-process (one per worker); it only ever holds short-lived bearer
# tokens and is purely a performance optimisation, never a correctness source.

class DarajaTokenCache:
    """Thread-safe, environment/credential-keyed Daraja OAuth token cache."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}  # key -> (token, expires_at_epoch)

    @staticmethod
    def _key(config):
        base_url = (config.get("DARAJA_BASE_URL") or "").lower()
        consumer_key = config.get("DARAJA_CONSUMER_KEY") or ""
        # Include the environment as a defensive belt-and-braces check so a
        # misconfigured sandbox/base-url combo still cannot reuse a token.
        env = (config.get("DARAJA_ENV") or "sandbox").lower()
        return (base_url, env, consumer_key)

    def get(self, config, *, skew=10.0):
        key = self._key(config)
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            token, expires_at = entry
            if now >= (expires_at - skew):
                # Expired (or within the safety skew): drop it.
                self._entries.pop(key, None)
                return None
            return token

    def set(self, config, token, ttl):
        key = self._key(config)
        with self._lock:
            self._entries[key] = (token, time.time() + float(ttl))

    def invalidate(self, config):
        key = self._key(config)
        with self._lock:
            self._entries.pop(key, None)

    def reset(self):
        with self._lock:
            self._entries.clear()


# Module-level singleton. Tests reset this between cases via
# ``MpesaService.reset_token_cache()``.
_token_cache = DarajaTokenCache()


# ---------------------------------------------------------------------------
# Global Daraja rate limiter + upstream cooldown
# ---------------------------------------------------------------------------
#
# Daraja's quota is application-wide, so every outbound Daraja call (OAuth, STK
# Push, STK Query, reconciliation, callback-triggered queries, sweeper, admin)
# is funnelled through one shared limiter. An in-process token bucket is used
# where PostgreSQL is unavailable (single process only); on PostgreSQL a
# database-backed token bucket row is shared across all workers/instances so
# the limiter is truly cross-process.
#
# A separate upstream cooldown is raised on 403/429/5xx so that a single global
# upstream incident does not inflate every pending transaction's attempt
# counter. Both mechanisms fail OPEN (admit the request) if the coordination
# store is unreachable, because stranding genuine deposits is worse than the
# (already cooldown-bounded) risk of briefly over-calling Daraja.

class _LocalThrottleState:
    """In-process token bucket + cooldown used when PostgreSQL is absent."""

    def __init__(self, capacity, refill_per_sec):
        self._lock = threading.Lock()
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.cooldown_until = 0.0
        self.cooldown_reason = None

    def _refill(self, now):
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.last_refill = now

    def cooldown_remaining(self, now=None):
        now = now if now is not None else time.monotonic()
        remaining = self.cooldown_until - now
        return max(0.0, remaining)

    def set_cooldown(self, seconds, reason):
        with self._lock:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + float(seconds))
            self.cooldown_reason = reason

    def acquire_permit(self, ignore_reconciliation_cooldown=False):
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            cooldown_blocks = self.cooldown_remaining(now) > 0 and not (
                ignore_reconciliation_cooldown
                and self.cooldown_reason in RECONCILIATION_SCOPED_COOLDOWN_REASONS
            )
            if cooldown_blocks:
                return False, self.cooldown_remaining(now)
            if self.tokens >= 1:
                self.tokens -= 1
                return True, 0.0
            deficit = 1 - self.tokens
            wait = deficit / self.refill_per_sec if self.refill_per_sec > 0 else 60.0
            return False, wait


# In-process singleton. Rebuilt from config by ``reset_daraja_throttle()`` so
# tests can install a tight budget without relying on test order.
_local_throttle = None
_local_throttle_lock = threading.Lock()

# Stable, application-specific namespace for per-transaction advisory locks so
# they can never collide with the sweeper leader advisory lock (which uses the
# single-argument form). Two-argument advisory locks are keyed by (classid,
# objid); we fix classid here and pass the transaction id as objid.
TX_ADVISORY_NAMESPACE = 0x4D50  # 'MP'

# Short-lived in-process deduplication of concurrent callback-triggered Daraja
# queries, keyed by checkout_request_id (see ``_callback_query``). Prevents two
# simultaneous callbacks for the same checkout from issuing duplicate Daraja
# queries. The global limiter is the cross-process safeguard; this is the
# same-process, same-checkout guard. An in-flight entry lives only for the
# duration of one query and is removed afterwards, so it is inherently
# short-lived and cannot accumulate.
_CALLBACK_DEDUP_GUARD = threading.Lock()
_callback_inflight = {}


def _mpesa_backoff_seconds(attempts, config):
    """Conservative, config-driven exponential backoff (no 24h waits).

    Applied only to *genuine* reconciliation outcomes (inconclusive / network),
    never to Daraja 403/429 (those are handled by the global upstream
    cooldown). ``last_reconciled_at`` is stored as naive UTC, so the elapsed
    time is computed with ``(datetime.utcnow() - last_reconciled_at)`` to stay
    timezone-safe under UTC, Africa/Nairobi and any host timezone.
    """
    base = 30
    max_backoff = 1800
    try:
        base = int(config.get("MPESA_BACKOFF_BASE_SECONDS", 30))
    except (TypeError, ValueError):
        base = 30
    try:
        max_backoff = int(config.get("MPESA_BACKOFF_MAX_SECONDS", 1800))
    except (TypeError, ValueError):
        max_backoff = 1800

    if base < 1:
        base = 1
    if max_backoff < base:
        max_backoff = base

    try:
        secs = min(max_backoff, base * (2 ** max(0, int(attempts))))
    except (OverflowError, ValueError):
        secs = max_backoff

    # Small jitter to avoid a thundering herd without exceeding the cap.
    jitter = random.uniform(0, max(1, secs * 0.1)) if secs > 0 else 0
    return int(secs + jitter)


# Daraja STK query ``ResultCode``s that are documented, definitive proof of
# cancellation/failure and therefore may move a deposit to ``FAILED``.
#
# Only ``1032`` ("Request cancelled by user") is treated as definitive. Any
# other non-zero/inconclusive code keeps the deposit in
# ``RECONCILIATION_PENDING`` so a genuine payment can never be stranded by a
# transient or unrecognised Daraja response. We deliberately do NOT add codes
# like ``1037`` (DS timeout) here: a timeout must never be taken as proof the
# customer did not pay.
DEFINITIVE_FAILURE_RESULT_CODES = frozenset({"1032"})

# Recorded on ``failure_reason`` when Daraja confirmed the payment but the
# untrusted callback metadata disagreed about the amount. This is explicitly NOT
# a definitive failure: Daraja said the money moved, so the deposit stays
# recoverable (and uncredited) instead of being stranded as ``FAILED``.
AMOUNT_MISMATCH_REASON = (
    "Amount mismatch between callback metadata and stored deposit amount."
)

# Recorded when no Daraja reference is available to key the ledger entry on. A
# deposit is never credited without an idempotency reference, because the
# ``unique_wallet_ledger_reference`` constraint could not then prevent a
# duplicate credit.
MISSING_REFERENCE_REASON = (
    "No Daraja reference available to guarantee a single wallet credit."
)


class MpesaService:

    # --- configuration -------------------------------------------------

    @staticmethod
    def _config():
        config = current_app.config

        missing = [key for key in REQUIRED_CONFIG_KEYS if not config.get(key)]

        if missing:
            # Log which keys are missing (names only, never values).
            current_app.logger.error(
                "M-Pesa configuration incomplete: %s", ", ".join(missing)
            )
            raise ApiError(
                "M-Pesa payments are not available at the moment.",
                503,
                ErrorCode.MPESA_NOT_CONFIGURED,
            )

        return config

    @staticmethod
    def _build_password(shortcode, passkey, timestamp):
        raw = f"{shortcode}{passkey}{timestamp}".encode("utf-8")

        return base64.b64encode(raw).decode("utf-8")

    # --- token cache + global rate limiter/cooldown control -----------

    @staticmethod
    def reset_token_cache():
        """Reset the process-wide Daraja token cache.

        Tests (and app instances) call this to guarantee environment/credential
        isolation: a freshly reset cache can never return a token cached under a
        different Daraja base URL or consumer key.
        """
        _token_cache.reset()

    @staticmethod
    def reset_daraja_throttle():
        """Rebuild the in-process rate-limiter/cooldown state from config.

        PostgreSQL-backed throttling state lives in the database. That table is
        not a SQLAlchemy model, so the test fixture's drop_all/create_all does
        not recreate it; without an explicit reset the budget row from a prior
        test would leak across tests. We therefore also reset the shared bucket
        row so a per-test budget is deterministic. (In production this method is
        only invoked by the test fixture, never at runtime.)
        """
        global _local_throttle
        config = current_app.config
        try:
            capacity = float(config.get("DARAJA_RATE_LIMIT_CAPACITY", 5))
            refill = float(config.get("DARAJA_RATE_LIMIT_REFILL_PER_SEC", 0.4167))
        except (TypeError, ValueError):
            capacity, refill = 5.0, 0.4167
        with _local_throttle_lock:
            _local_throttle = _LocalThrottleState(capacity, refill)

        if MpesaService._use_postgres_throttle():
            try:
                MpesaService._pg_reset_throttle_row(capacity, refill)
            except Exception:
                current_app.logger.exception(
                    "MPESA_EVENT=DARAJA_THROTTLE_RESET_ERROR"
                )

    @staticmethod
    def _local_throttle_state():
        global _local_throttle
        if _local_throttle is None:
            MpesaService.reset_daraja_throttle()
        return _local_throttle

    @staticmethod
    def _use_postgres_throttle():
        try:
            return db.engine.dialect.name == "postgresql"
        except Exception:
            return False

    @staticmethod
    def _daraja_cooldown_remaining(config, domain=DARAJA_DOMAIN_RECONCILIATION):
        """Seconds left on the global upstream cooldown (0.0 if none).

        ``domain`` scopes the cooldown: a reconciliation-scoped cooldown (a bare
        ``daraja_5xx`` seen while polling a stale transaction) is invisible to
        ``initiation`` calls, so one user's stuck deposit can never block another
        user's brand-new STK Push. 429/403 cooldowns remain application-wide.
        """
        remaining, reason = MpesaService._daraja_cooldown_state()
        if remaining <= 0:
            return 0.0
        if (
            domain == DARAJA_DOMAIN_INITIATION
            and reason in RECONCILIATION_SCOPED_COOLDOWN_REASONS
        ):
            return 0.0
        return remaining

    @staticmethod
    def _daraja_cooldown_state():
        """Return ``(remaining_seconds, reason)`` for the global cooldown."""
        if MpesaService._use_postgres_throttle():
            return MpesaService._pg_cooldown_remaining()
        state = MpesaService._local_throttle_state()
        return state.cooldown_remaining(), state.cooldown_reason

    @staticmethod
    def _daraja_set_cooldown(config, seconds, reason):
        """Enter a global upstream cooldown (403/429/5xx)."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 30.0
        if seconds < 0:
            seconds = 0.0
        if MpesaService._use_postgres_throttle():
            MpesaService._pg_set_cooldown(seconds, reason)
        else:
            MpesaService._local_throttle_state().set_cooldown(seconds, reason)
        current_app.logger.error(
            "MPESA_EVENT=DARAJA_COOLDOWN_STARTED seconds=%s reason=%s",
            int(seconds),
            reason,
        )

    @staticmethod
    def _daraja_acquire_permit(config, what, *, domain=DARAJA_DOMAIN_RECONCILIATION):
        """Acquire one shared Daraja permit. Returns (granted, retry_after).

        An ``initiation`` call ignores a reconciliation-scoped cooldown (a bare
        ``daraja_5xx``) at the permit layer too, so a stale transaction's failed
        poll cannot starve a brand-new STK Push. The shared token bucket still
        applies to every call.
        """
        ignore_reconciliation_cooldown = domain == DARAJA_DOMAIN_INITIATION
        if MpesaService._use_postgres_throttle():
            return MpesaService._pg_acquire_permit(
                config,
                ignore_reconciliation_cooldown=ignore_reconciliation_cooldown,
            )
        return MpesaService._local_throttle_state().acquire_permit(
            ignore_reconciliation_cooldown=ignore_reconciliation_cooldown
        )

    # --- PostgreSQL-backed shared token bucket (cross-process) ----------

    _PG_THROTTLE_TABLE = "daraja_throttle"
    _PG_THROTTLE_ID = 1

    @staticmethod
    def _pg_ensure_throttle_row(conn):
        """Create the single shared limiter row if it does not yet exist.

        Capacity/refill come from config at creation time; a focused test that
        wants a tight budget must set the config before the first acquire.
        """
        config = current_app.config
        try:
            capacity = float(config.get("DARAJA_RATE_LIMIT_CAPACITY", 5))
            refill = float(config.get("DARAJA_RATE_LIMIT_REFILL_PER_SEC", 0.4167))
        except (TypeError, ValueError):
            capacity, refill = 5.0, 0.4167

        # Self-heal: create the shared limiter table if it is missing (e.g. when
        # the schema is built via ``db.create_all()`` instead of the Alembic
        # migration). Idempotent and schema-compatible with migration
        # a1b2c3d4e5f6 so it is a no-op wherever the migration already ran.
        ts_type = (
            "TIMESTAMP WITH TIME ZONE"
            if conn.dialect.name == "postgresql"
            else "TIMESTAMP"
        )
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS daraja_throttle ("
                f"id INTEGER PRIMARY KEY, "
                f"tokens NUMERIC(12,4) NOT NULL, "
                f"last_refill {ts_type} NOT NULL, "
                f"capacity NUMERIC(12,4) NOT NULL, "
                f"refill_per_sec NUMERIC(18,6) NOT NULL, "
                f"cooldown_until {ts_type}, "
                f"cooldown_reason VARCHAR(50))"
            )
        )

        conn.execute(
            text(
                "INSERT INTO daraja_throttle "
                "(id, tokens, last_refill, capacity, refill_per_sec) "
                "VALUES (:id, :cap, now(), :cap, :ref) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": MpesaService._PG_THROTTLE_ID,
                "cap": capacity,
                "ref": refill,
            },
        )

    @staticmethod
    def _pg_reset_throttle_row(capacity, refill):
        """Recreate the shared limiter row with the given budget.

        Used by ``reset_daraja_throttle()`` so each test starts from a fresh,
        deterministic bucket. Idempotent on the table schema.
        """
        with db.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            MpesaService._pg_ensure_throttle_row(conn)
            conn.execute(
                text("DELETE FROM daraja_throttle WHERE id = :id"),
                {"id": MpesaService._PG_THROTTLE_ID},
            )
            conn.execute(
                text(
                    "INSERT INTO daraja_throttle "
                    "(id, tokens, last_refill, capacity, refill_per_sec) "
                    "VALUES (:id, :cap, now(), :cap, :ref)"
                ),
                {
                    "id": MpesaService._PG_THROTTLE_ID,
                    "cap": capacity,
                    "ref": refill,
                },
            )

    @staticmethod
    def _pg_cooldown_remaining():
        """Return ``(remaining_seconds, reason)`` for the shared cooldown row."""
        try:
            conn = db.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            )
            try:
                row = conn.execute(
                    text(
                        "SELECT GREATEST(0, EXTRACT(EPOCH FROM "
                        "(cooldown_until - now()))), cooldown_reason "
                        "FROM daraja_throttle WHERE id = :id"
                    ),
                    {"id": MpesaService._PG_THROTTLE_ID},
                ).first()
                if row is None:
                    return 0.0, None
                return float(row[0] or 0.0), row[1]
            finally:
                conn.close()
        except Exception:
            current_app.logger.exception(
                "MPESA_EVENT=DARAJA_THROTTLE_READ_ERROR"
            )
            return 0.0, None

    @staticmethod
    def _pg_set_cooldown(seconds, reason):
        try:
            conn = db.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            )
            try:
                MpesaService._pg_ensure_throttle_row(conn)
                conn.execute(
                    text(
                        "UPDATE daraja_throttle SET "
                        "cooldown_until = GREATEST(cooldown_until, "
                        "now() + (:secs * INTERVAL '1 second')), "
                        "cooldown_reason = :reason "
                        "WHERE id = :id"
                    ),
                    {
                        "secs": float(seconds),
                        "reason": reason,
                        "id": MpesaService._PG_THROTTLE_ID,
                    },
                )
            finally:
                conn.close()
        except Exception:
            current_app.logger.exception("MPESA_EVENT=DARAJA_COOLDOWN_ERROR")

    @staticmethod
    def _pg_acquire_permit(config, ignore_reconciliation_cooldown=False):
        """Atomically consume one token from the shared bucket (fail open).

        ``capacity``/``refill_per_sec`` are the rate-limit *policy* and are read
        live from config (so a deployment can change the budget without a DB
        migration and tests can install a tight budget); the row only holds the
        shared mutable token state (``tokens``/``last_refill``) and the global
        cooldown.

        ``ignore_reconciliation_cooldown`` lets an ``initiation`` call pass a
        reconciliation-scoped cooldown (a bare ``daraja_5xx``) so a stale
        transaction's failed poll cannot starve a brand-new STK Push. The token
        bucket itself still applies.
        """
        try:
            capacity = float(config.get("DARAJA_RATE_LIMIT_CAPACITY", 5))
            refill = float(config.get("DARAJA_RATE_LIMIT_REFILL_PER_SEC", 0.4167))
        except (TypeError, ValueError):
            capacity, refill = 5.0, 0.4167
        recon_reasons = list(RECONCILIATION_SCOPED_COOLDOWN_REASONS)
        try:
            conn = db.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            )
            try:
                MpesaService._pg_ensure_throttle_row(conn)
                granted = (
                    conn.execute(
                        text(
                            "UPDATE daraja_throttle SET "
                            "tokens = LEAST(:cap, tokens + EXTRACT(EPOCH FROM "
                            "(now() - last_refill)) * :ref) - 1, "
                            "last_refill = now() "
                            "WHERE id = :id "
                            "  AND (cooldown_until IS NULL OR cooldown_until <= now() "
                            "       OR (:ignore_recon AND cooldown_reason = ANY(:recon_reasons))) "
                            "  AND LEAST(:cap, tokens + EXTRACT(EPOCH FROM "
                            "(now() - last_refill)) * :ref) >= 1 "
                            "RETURNING 1"
                        ),
                        {
                            "id": MpesaService._PG_THROTTLE_ID,
                            "cap": capacity,
                            "ref": refill,
                            "ignore_recon": bool(ignore_reconciliation_cooldown),
                            "recon_reasons": recon_reasons,
                        },
                    ).rowcount
                    == 1
                )
                if granted:
                    return True, 0.0
                row = conn.execute(
                    text(
                        "SELECT GREATEST(0, EXTRACT(EPOCH FROM "
                        "(cooldown_until - now()))), tokens, "
                        "EXTRACT(EPOCH FROM (now() - last_refill)), cooldown_reason "
                        "FROM daraja_throttle WHERE id = :id"
                    ),
                    {"id": MpesaService._PG_THROTTLE_ID},
                ).first()
                cooldown_rem, tokens, since_refill, cooldown_reason = row or (
                    0, 0, 0, None,
                )
                tokens = float(tokens or 0)
                since_refill = float(since_refill or 0)
                if (
                    ignore_reconciliation_cooldown
                    and cooldown_reason in RECONCILIATION_SCOPED_COOLDOWN_REASONS
                ):
                    cooldown_rem = 0
                deficit = 1 - min(capacity, tokens + since_refill * refill)
                wait = deficit / refill if refill else 60.0
                return False, max(float(cooldown_rem), float(wait))
            finally:
                conn.close()
        except Exception:
            current_app.logger.exception("MPESA_EVENT=DARAJA_THROTTLE_ERROR")
            return True, 0.0

    # --- low-level Daraja HTTP -----------------------------------------

    @staticmethod
    def _request_json(
        method, url, *, config, what,
        domain=DARAJA_DOMAIN_RECONCILIATION, **kwargs
    ):

        """Issue a Daraja HTTP call and return parsed JSON, classifying failure.

        Every failure is translated into a generic, frontend-safe ``ApiError``.
        The server log carries the diagnostic detail (HTTP status, truncated
        upstream body, environment, exception class) required to tell the
        upstream failure categories apart:

        * ``401`` / ``403`` / ``404`` / ``429`` / ``5xx``  (HTTP error status)
        * ``timeout`` / ``connection`` / ``request`` (network/TLS)
        * ``non-json`` (a 2xx/error response whose body is not JSON, e.g. an
          HTML gateway or maintenance page)

        No secret is ever logged: consumer key/secret, access token, the
        ``Authorization`` header and the passkey are never written to the log,
        and the full URL is reduced to its path before logging.
        """
        env = config.get("DARAJA_ENV", "sandbox")
        http = requests.get if method == "GET" else requests.post

        # 1) Global upstream cooldown: if Daraja is in a cooldown (403/429/5xx
        #    seen cluster-wide), do NOT make another outbound request and do NOT
        #    charge this as a per-transaction attempt. Fail the call safely.
        #    ``domain`` scopes this: a reconciliation-scoped cooldown (a bare
        #    ``daraja_5xx`` from polling a stale transaction) never gates an
        #    ``initiation`` call, keeping the two failure domains independent.
        cooldown_remaining = MpesaService._daraja_cooldown_remaining(
            config, domain=domain
        )
        if cooldown_remaining > 0:
            current_app.logger.error(
                "MPESA_EVENT=DARAJA_REQUEST_SKIPPED what=%s env=%s "
                "reason=global_cooldown remaining_seconds=%s",
                what,
                env,
                int(cooldown_remaining),
            )
            raise DarajaUpstreamCooldown(
                http_status=429,
                retry_after=cooldown_remaining,
                reason="global_cooldown",
            )

        # 2) Global shared rate limiter: one application-wide choke point for
        #    every Daraja call (OAuth, STK Push, STK Query, reconciliation,
        #    callback, sweeper, admin). Cross-process on PostgreSQL.
        granted, retry_after = MpesaService._daraja_acquire_permit(
            config, what, domain=domain
        )
        if not granted:
            current_app.logger.error(
                "MPESA_EVENT=DARAJA_REQUEST_SKIPPED what=%s env=%s "
                "reason=rate_limited retry_after=%s",
                what,
                env,
                int(retry_after) if retry_after else None,
            )
            raise DarajaThrottled(retry_after=retry_after, reason="rate_limited")

        try:
            response = http(url, timeout=config["DARAJA_TIMEOUT"], **kwargs)
        except requests.Timeout:
            current_app.logger.error(
                "MPESA_EVENT=DARAJA_HTTP_ERROR what=%s env=%s category=timeout",
                what,
                env,
            )
            raise ApiError(
                "Could not reach M-Pesa. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )
        except requests.ConnectionError as error:
            current_app.logger.error(
                "MPESA_EVENT=DARAJA_HTTP_ERROR what=%s env=%s category=connection "
                "error=%s",
                what,
                env,
                type(error).__name__,
            )
            raise ApiError(
                "Could not reach M-Pesa. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )
        except requests.RequestException as error:
            current_app.logger.error(
                "MPESA_EVENT=DARAJA_HTTP_ERROR what=%s env=%s category=request "
                "error=%s",
                what,
                env,
                type(error).__name__,
            )
            raise ApiError(
                "Could not reach M-Pesa. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        # HTTP error status: classify and, for global upstream signals
        # (429/403/5xx), enter a cluster-wide cooldown so one incident does not
        # inflate every pending transaction's attempt counter.
        if response.status_code >= 400:
            MpesaService._log_daraja_http_error(what, url, response, env)

            status = response.status_code
            retry_after = None

            # STK Query only: a 4xx/5xx whose JSON body carries a Daraja
            # ``errorCode`` for a SPECIFIC checkout request (the ``500.001.xxxx``
            # family) describes that one transaction, not the health of the
            # Daraja API. Handle it per-transaction and NEVER trip the shared
            # upstream cooldown that would block every other in-flight deposit.
            if what == "stk-query":
                verdict, error_code, error_message = (
                    MpesaService._classify_stk_query_error(response)
                )
                if verdict == "unknown":
                    current_app.logger.warning(
                        "MPESA_EVENT=DARAJA_QUERY_TRANSACTION_UNKNOWN "
                        "env=%s status=%s error_code=%s",
                        env,
                        status,
                        error_code,
                    )
                    raise DarajaTransactionUnknown(
                        error_code=error_code, error_message=error_message
                    )
                if verdict == "processing":
                    # Transient: the customer has not yet acted on the STK
                    # prompt. Keep the deposit recoverable, charge an attempt,
                    # but do NOT cooldown.
                    current_app.logger.info(
                        "MPESA_EVENT=DARAJA_QUERY_TRANSACTION_PROCESSING "
                        "env=%s status=%s error_code=%s",
                        env,
                        status,
                        error_code,
                    )
                    raise DarajaHttpError(
                        "M-Pesa is still processing this request.",
                        http_status=status,
                    )

            if status == 429:
                # Honour Retry-After when present (capped defensively).
                raw_retry = None
                try:
                    raw_retry = response.headers.get("Retry-After")
                except Exception:
                    raw_retry = None
                retry_after = MpesaService._parse_retry_after(raw_retry, config)
                cooldown = max(
                    retry_after or 0,
                    config.get("DARAJA_COOLDOWN_429_SECONDS", 30),
                )
                cooldown = min(
                    cooldown, config.get("DARAJA_COOLDOWN_429_MAX_SECONDS", 120)
                )
                MpesaService._daraja_set_cooldown(
                    config, cooldown, "daraja_429"
                )
                raise DarajaUpstreamCooldown(
                    http_status=429, retry_after=retry_after, reason="daraja_429"
                )
            if status == 403:
                # Likely Imperva/WAF. Longer cooldown; do NOT auto-retry the
                # request (a fresh token cannot clear a WAF block).
                MpesaService._daraja_set_cooldown(
                    config,
                    config.get("DARAJA_COOLDOWN_403_SECONDS", 300),
                    "daraja_403",
                )
                raise DarajaUpstreamCooldown(
                    http_status=403, reason="daraja_403"
                )
            if 500 <= status < 600:
                MpesaService._daraja_set_cooldown(
                    config,
                    config.get("DARAJA_COOLDOWN_5XX_SECONDS", 30),
                    "daraja_5xx",
                )
                raise DarajaUpstreamCooldown(
                    http_status=status, reason="daraja_5xx"
                )
            # 401 (stale token) / 404 / other: raise a plain DarajaHttpError so
            # the caller (query/stk) can invalidate the token and retry once.
            raise DarajaHttpError(
                "Could not reach M-Pesa. Please try again.",
                http_status=status,
            )

        # Success status but body is not JSON (HTML gateway/error page, etc.).
        try:
            data = response.json()
        except ValueError:
            current_app.logger.error(
                "MPESA_EVENT=DARAJA_HTTP_ERROR what=%s env=%s status=%s "
                "category=non-json",
                what,
                env,
                response.status_code,
            )
            raise ApiError(
                "Could not reach M-Pesa. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        if not isinstance(data, dict):
            data = {}

        return data

    @staticmethod
    def _classify_stk_query_error(response):
        """Classify an STK Query HTTP-error body as a per-transaction signal.

        Safaricom's STK Push *Query* endpoint reports the state of a single
        checkout request by returning HTTP 500 with a JSON body such as
        ``{"errorCode": "500.001.1001", "errorMessage": "The transaction does
        not exist"}``. That is a statement about one transaction, not the Daraja
        API, so it must be handled per-transaction rather than tripping the
        shared upstream cooldown.

        Returns ``(verdict, error_code, error_message)`` where ``verdict`` is:

        * ``"unknown"``    - the checkout request id is unknown/expired
                             (TERMINAL for this transaction: stop polling it);
        * ``"processing"`` - the STK prompt is still in flight (TRANSIENT: keep
                             the deposit recoverable and retry later);
        * ``None``         - not a recognised per-transaction signal; the caller
                             falls back to normal HTTP-status handling (a real
                             5xx still enters the reconciliation-scoped
                             cooldown).
        """
        try:
            body = response.json()
        except ValueError:
            return None, None, None
        if not isinstance(body, dict):
            return None, None, None

        error_code = str(body.get("errorCode") or "").strip()
        error_message = str(body.get("errorMessage") or "").strip()
        haystack = error_message.lower()

        configured = current_app.config.get("DARAJA_QUERY_TRANSACTION_ERROR_CODES")
        known_codes = (
            frozenset(configured)
            if configured
            else DARAJA_QUERY_TRANSACTION_ERROR_CODES
        )
        is_query_family = (
            error_code in known_codes
            or error_code.startswith(DARAJA_QUERY_TRANSACTION_ERROR_PREFIX)
        )

        if any(
            marker in haystack
            for marker in DARAJA_TRANSACTION_PROCESSING_MESSAGE_MARKERS
        ):
            return "processing", error_code or None, error_message or None
        if any(
            marker in haystack
            for marker in DARAJA_TRANSACTION_UNKNOWN_MESSAGE_MARKERS
        ):
            return "unknown", error_code or None, error_message or None
        if is_query_family:
            # A recognised per-transaction error family but an unfamiliar
            # message: take the SAFE reading (keep the deposit recoverable)
            # rather than stranding a possibly genuine payment.
            return "processing", error_code or None, error_message or None
        return None, None, None

    @staticmethod
    def _parse_retry_after(raw, config):
        """Parse an upstream ``Retry-After`` value into a bounded seconds float.

        Accepts either an integer number of seconds or a HTTP-date. Returns
        ``None`` when missing/unparseable. Capped by
        ``DARAJA_COOLDOWN_429_MAX_SECONDS`` so a hostile or buggy value cannot
        park the limiter forever.
        """
        if raw is None:
            return None
        try:
            return min(
                float(raw), config.get("DARAJA_COOLDOWN_429_MAX_SECONDS", 120)
            )
        except (TypeError, ValueError):
            pass
        # HTTP-date form, e.g. "Wed, 21 Oct 2026 07:28:00 GMT".
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(str(raw))
            if when is None:
                return None
            from datetime import datetime as _dt

            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.utcnow().tzinfo)
            delta = (when - _dt.now(when.tzinfo)).total_seconds()
            if delta <= 0:
                return None
            return min(
                delta, config.get("DARAJA_COOLDOWN_429_MAX_SECONDS", 120)
            )
        except Exception:
            return None

    @staticmethod
    def _log_daraja_http_error(what, url, response, env):
        """Log an upstream Daraja HTTP error without leaking secrets.

        Logs the status code, the request path (never the full URL), the
        environment and a *truncated* copy of the upstream body. The body is
        upstream error detail (never our credentials) but is capped so a large
        or malformed payload cannot flood the logs.
        """
        try:
            raw = response.text
        except Exception:
            raw = ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        body = (raw or "")[-500:]

        current_app.logger.error(
            "MPESA_EVENT=DARAJA_HTTP_ERROR what=%s env=%s status=%s url_path=%s "
            "body=%s",
            what,
            env,
            response.status_code,
            _url_path(url),
            body,
        )

    @staticmethod
    def validate_daraja_config(app):
        """Validate Daraja/M-Pesa configuration at application startup.

        Safe-by-default: an invalid ``DARAJA_ENV`` is normalised to ``sandbox``
        (the safe default) with a warning rather than crashing boot, and an
        entirely unconfigured deployment (all values empty) only logs a warning
        — M-Pesa endpoints then return ``503`` at request time via
        :meth:`_config`. A *partially* configured deployment (some values set,
        others missing/empty) is a likely misconfiguration and is logged as an
        error so an operator can spot it.

        Set ``DARAJA_REQUIRE_CONFIG=true`` to turn a partial/empty
        configuration into a hard startup failure instead of a warning.

        Environment/endpoint mismatches — production env pointing at the sandbox
        host, or sandbox env pointing at the production host — are logged as
        errors because they are the classic cause of silent authentication
        failures (sandbox credentials sent to production, or vice versa).
        """
        config = app.config
        env = (config.get("DARAJA_ENV") or "sandbox").strip().lower()

        if env not in ALLOWED_DARAJA_ENVS:
            app.logger.error(
                "MPESA_EVENT=DARAJA_CONFIG_INVALID_ENV env=%s "
                "reason=unknown_env defaulting_to=sandbox",
                env,
            )
            config["DARAJA_ENV"] = "sandbox"
            env = "sandbox"

        base_url = (config.get("DARAJA_BASE_URL") or "").lower()
        if env == "production" and SANDBOX_HOST in base_url:
            app.logger.error(
                "MPESA_EVENT=DARAJA_CONFIG_ENV_MISMATCH env=production "
                "base_url_is_sandbox=true "
                "reason=sandbox_credentials_would_be_sent_to_production"
            )
        elif env == "sandbox" and PRODUCTION_HOST in base_url:
            app.logger.error(
                "MPESA_EVENT=DARAJA_CONFIG_ENV_MISMATCH env=sandbox "
                "base_url_is_production=true "
                "reason=production_credentials_would_be_sent_to_sandbox"
            )

        configured = [k for k in REQUIRED_CONFIG_KEYS if config.get(k)]
        missing = [k for k in REQUIRED_CONFIG_KEYS if not config.get(k)]

        if not configured:
            app.logger.warning(
                "MPESA_EVENT=DARAJA_CONFIG_NOT_CONFIGURED "
                "reason=all_values_empty mpesa_endpoints_will_return_503"
            )
            return

        if missing:
            app.logger.error(
                "MPESA_EVENT=DARAJA_CONFIG_INCOMPLETE missing=%s",
                ", ".join(missing),
            )
            if str(config.get("DARAJA_REQUIRE_CONFIG", "")).lower() in (
                "1",
                "true",
                "yes",
            ):
                raise RuntimeError(
                    "Daraja M-Pesa configuration is incomplete: "
                    + ", ".join(missing)
                )

    # --- Daraja calls --------------------------------------------------

    @staticmethod
    def get_access_token(force_refresh=False, domain=DARAJA_DOMAIN_RECONCILIATION):
        """Request (or return a cached) Daraja OAuth access token.

        The cache is keyed by Daraja base URL + consumer key (see
        :class:`DarajaTokenCache`) so sandbox and production credentials can
        never share a token, and a reset clears every environment at once.

        ``domain`` is forwarded to :meth:`_request_json` so a token fetched for
        a user-facing STK Push (``initiation``) is not blocked by a
        reconciliation-scoped upstream cooldown.
        """
        config = MpesaService._config()

        if not force_refresh:
            cached = _token_cache.get(config)
            if cached is not None:
                return cached

        url = f"{config['DARAJA_BASE_URL']}{TOKEN_PATH}"
        data = MpesaService._request_json(
            "GET",
            url,
            config=config,
            what="access-token",
            domain=domain,
            auth=(
                config["DARAJA_CONSUMER_KEY"],
                config["DARAJA_CONSUMER_SECRET"],
            ),
        )

        token = (data or {}).get("access_token")
        expires_in = (data or {}).get("expires_in")

        if not token:
            current_app.logger.error(
                "MPESA_EVENT=DARAJA_TOKEN_MISSING env=%s",
                config.get("DARAJA_ENV", "sandbox"),
            )
            raise ApiError(
                "Could not reach M-Pesa. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        # Cache token with a conservative TTL. If Daraja does not return
        # expires_in, fall back to 50 minutes.
        ttl = None
        try:
            if isinstance(expires_in, (int, float)):
                ttl = float(expires_in)
            elif isinstance(expires_in, str) and expires_in.isdigit():
                ttl = float(expires_in)
        except Exception:
            ttl = None

        if ttl is None:
            ttl = 50 * 60

        _token_cache.set(config, token, ttl)

        return token

    @staticmethod
    def _authed_request(
        method, url, *, config, what, json=None,
        domain=DARAJA_DOMAIN_RECONCILIATION,
    ):
        """Make a Daraja call that needs a bearer token, with one safe retry.

        If Daraja rejects the (cached) token with a genuine ``401``, the cached
        token is invalidated and exactly one fresh token is fetched and retried.
        This is bounded: original request -> invalidate -> one fresh token ->
        one retry -> stop. Cooldown/rate-limit errors (``DarajaUpstreamCooldown``,
        ``DarajaThrottled``) are never retried here — they propagate so the
        caller can keep the deposit recoverable without charging an attempt.

        ``domain`` (``initiation`` for the user-facing STK Push,
        ``reconciliation`` for every query/sweeper path) is forwarded to the
        token fetch and the request so the two failure domains stay independent.
        """
        token = MpesaService.get_access_token(domain=domain)
        try:
            return MpesaService._request_json(
                method,
                url,
                config=config,
                what=what,
                domain=domain,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except DarajaHttpError as error:
            if (
                error.http_status == 401
                and not isinstance(error, DarajaUpstreamCooldown)
                and not isinstance(error, DarajaTransactionUnknown)
            ):
                _token_cache.invalidate(config)
                token = MpesaService.get_access_token(
                    force_refresh=True, domain=domain
                )
                return MpesaService._request_json(
                    method,
                    url,
                    config=config,
                    what=what,
                    domain=domain,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            raise

    @staticmethod
    def send_stk_push(amount, phone, account_reference):
        """Send the STK Push request and return the Daraja response payload."""
        config = MpesaService._config()

        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        shortcode = str(config["DARAJA_SHORTCODE"])

        payload = {
            "BusinessShortCode": shortcode,
            "Password": MpesaService._build_password(
                shortcode, config["DARAJA_PASSKEY"], timestamp
            ),
            "Timestamp": timestamp,
            "TransactionType": config["DARAJA_TRANSACTION_TYPE"],
            # Daraja expects a whole-number amount.
            "Amount": int(amount),
            "PartyA": phone,
            "PartyB": shortcode,
            "PhoneNumber": phone,
            "CallBackURL": config["DARAJA_CALLBACK_URL"],
            "AccountReference": account_reference,
            "TransactionDesc": "Vyloc wallet deposit",
        }

        url = f"{config['DARAJA_BASE_URL']}{STK_PUSH_PATH}"
        return MpesaService._authed_request(
            "POST", url, config=config, what="stk-push", json=payload,
            domain=DARAJA_DOMAIN_INITIATION,
        )

    @staticmethod
    def query_stk_status(checkout_request_id):
        """Reconcile an STK Push with Daraja using the backend's own credentials.

        The callback endpoint is unauthenticated, so its ``ResultCode`` must
        never be trusted as proof of payment. This server-to-server query is
        authenticated with the consumer key/secret that only the backend holds,
        making it the authoritative source for whether the payment actually
        succeeded. ``checkout_request_id`` is supplied by Daraja, not the client.

        Every outbound Daraja call (including this one) passes through the
        shared global rate limiter and upstream cooldown in :meth:`_request_json`,
        so the callback path is protected exactly like the sweeper/admin/user
        paths.
        """
        config = MpesaService._config()
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        shortcode = str(config["DARAJA_SHORTCODE"])
        password = MpesaService._build_password(
            shortcode, config["DARAJA_PASSKEY"], timestamp
        )
        payload = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }

        url = f"{config['DARAJA_BASE_URL']}{STK_QUERY_PATH}"
        return MpesaService._authed_request(
            "POST", url, config=config, what="stk-query", json=payload,
            domain=DARAJA_DOMAIN_RECONCILIATION,
        )

    @staticmethod
    def reject_unauthorized_source(request):
        """Optional defence-in-depth for the unauthenticated callback.

        If ``DARAJA_CALLBACK_ALLOWED_IPS`` is configured, reject callbacks from
        any other source IP. This is NOT cryptographic proof of payment — the
        authoritative check is :meth:`query_stk_status`; IP allowlisting only
        narrows the set of hosts that can trigger reconciliation.
        """
        allowed = current_app.config.get("DARAJA_CALLBACK_ALLOWED_IPS")
        if not allowed:
            return

        client_ip = request.remote_addr
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()

        if client_ip not in allowed:
            current_app.logger.warning(
                "Rejected M-Pesa callback from unauthorized source %s", client_ip
            )
            raise ApiError("Forbidden", 403, ErrorCode.FORBIDDEN)

    @staticmethod
    def initiate_deposit(user, amount, phone):
        """Start an M-Pesa deposit for the authenticated user.

        R2: Daraja is called *first* (no database transaction is held open
        across the slow external call). The pending ``mpesa_transactions`` row
        is then persisted once, already carrying the ``checkout_request_id``,
        so the callback can always correlate it and the deposit can never
        become unmatchable. The wallet is never credited here; crediting only
        happens once Daraja confirms the payment via reconciliation.
        """
        amount = to_money(amount)

        wallet = WalletService.get_wallet_or_error(
            user.id, message="Wallet not found for this account."
        )
        account_reference = generate_account_reference()

        current_app.logger.info(
            "MPESA_EVENT=STK_PUSH_REQUESTED user=%s wallet=%s amount=%s "
            "account_reference=%s phone=%s",
            user.id,
            wallet.id,
            amount,
            account_reference,
            mask_phone_number(phone),
        )

        try:
            response = MpesaService.send_stk_push(
                amount=amount,
                phone=phone,
                account_reference=account_reference,
            )
        except ApiError as error:
            current_app.logger.error(
                "MPESA_EVENT=STK_PUSH_FAILED user=%s account_reference=%s "
                "reason=%s",
                user.id,
                account_reference,
                error.message,
            )
            MpesaService._persist_failed_attempt(
                user, wallet, account_reference, phone, amount, error.message
            )
            raise

        response_code = str(response.get("ResponseCode", ""))

        if response_code != "0":
            failure_reason = (
                response.get("errorMessage")
                or response.get("ResponseDescription")
                or "M-Pesa request was not accepted."
            )
            current_app.logger.error(
                "MPESA_EVENT=STK_PUSH_REJECTED user=%s response_code=%s",
                user.id,
                response_code,
            )
            MpesaService._persist_failed_attempt(
                user, wallet, account_reference, phone, amount, failure_reason
            )
            current_app.logger.error(
                "Daraja rejected STK push for user=%s", user.id
            )
            raise ApiError(
                "M-Pesa request was not accepted. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        mpesa_transaction = MpesaTransaction(
            user_id=user.id,
            wallet_id=wallet.id,
            account_reference=account_reference,
            phone_number=phone,
            amount=amount,
            status=MpesaTransactionStatus.PENDING,
            merchant_request_id=truncate(response.get("MerchantRequestID"), 64),
            checkout_request_id=truncate(response.get("CheckoutRequestID"), 64),
            result_desc=truncate(
                response.get("CustomerMessage") or response.get("ResponseDescription"),
                RESULT_DESC_MAX_LENGTH,
            ),
        )

        try:
            db.session.add(mpesa_transaction)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Could not persist M-Pesa deposit request")
            raise ApiError(
                "Deposit could not be initiated.",
                500,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        current_app.logger.info(
            "MPESA_EVENT=STK_PUSH_ACCEPTED user=%s mpesa_transaction=%s "
            "checkout_request_id=%s merchant_request_id=%s",
            user.id,
            mpesa_transaction.id,
            mpesa_transaction.checkout_request_id,
            mpesa_transaction.merchant_request_id,
        )
        current_app.logger.info(
            "MPESA_EVENT=MPESA_TRANSACTION_CREATED mpesa_transaction=%s "
            "checkout_request_id=%s status=%s amount=%s",
            mpesa_transaction.id,
            mpesa_transaction.checkout_request_id,
            mpesa_transaction.status,
            mpesa_transaction.amount,
        )

        return mpesa_transaction

    @staticmethod
    def _persist_failed_attempt(
        user, wallet, account_reference, phone, amount, reason
    ):
        """Record a failed STK initiation attempt for auditability.

        Failed attempts have no ``checkout_request_id`` (Daraja did not accept
        the push), so no callback will ever correlate them.
        """
        try:
            failed = MpesaTransaction(
                user_id=user.id,
                wallet_id=wallet.id,
                account_reference=account_reference,
                phone_number=phone,
                amount=amount,
                status=MpesaTransactionStatus.FAILED,
                result_desc=truncate(reason, RESULT_DESC_MAX_LENGTH),
            )
            db.session.add(failed)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Could not persist failed M-Pesa attempt")

    # --- callback ------------------------------------------------------

    @staticmethod
    def _lock_mpesa_transaction(mpesa_transaction_id):
        """Re-read one deposit row under a row lock, discarding stale state.

        ``SELECT ... FOR UPDATE`` serialises every writer for this deposit, and
        ``populate_existing()`` forces the in-session copy to be refreshed from
        the row that was just locked. Together they mean a caller can never act
        on a status another worker has already changed: the caller either waits
        for that worker to commit or reads its committed result.

        Returns ``None`` when the row no longer exists.
        """
        return (
            MpesaTransaction.query.populate_existing()
            .filter_by(id=mpesa_transaction_id)
            .with_for_update()
            .first()
        )

    @staticmethod
    def _record_reconciliation_attempt(mpesa_transaction, query_result=None):
        """Persist observability metadata for a reconciliation attempt."""
        mpesa_transaction.reconciliation_attempts = (
            mpesa_transaction.reconciliation_attempts or 0
        ) + 1
        mpesa_transaction.last_reconciled_at = datetime.utcnow()

        if query_result is not None:
            mpesa_transaction.query_result_code = truncate(
                str(query_result.get("ResultCode")), 10
            )
            mpesa_transaction.query_result_desc = truncate(
                query_result.get("ResultDesc"), RESULT_DESC_MAX_LENGTH
            )

    @staticmethod
    def process_callback(parsed_callback):
        """Process a Daraja STK callback exactly once per terminal state.

        The unauthenticated callback is treated only as a notification. Before
        any wallet is credited, the payment is reconciled with Daraja using the
        backend's own credentials so a forged callback cannot manufacture money.

        State handling (R1/R2 remediation):
        * COMPLETED / FAILED are terminal and ignored (idempotency guard).
        * PENDING and RECONCILIATION_PENDING are (re)processed:
          - query ResultCode 0        -> credit exactly once, COMPLETED
          - a recognised definitive failure (e.g. 1032) -> FAILED
          - any other non-zero/inconclusive/error -> RECONCILIATION_PENDING

        A deposit is never marked FAILED merely because the reconciliation query
        was inconclusive; unknown outcomes remain recoverable. A confirmed
        payment whose callback metadata reports a different amount is likewise
        left recoverable and uncredited rather than FAILED.

        Returns the matching ``MpesaTransaction`` or ``None`` when the callback
        does not belong to a known deposit. A repeated callback never credits
        the wallet twice.
        """
        checkout_request_id = parsed_callback["checkout_request_id"]

        # Phase 1 — locate the deposit with an ORDINARY read (no row lock). The
        # untrusted callback is only a notification; we need its id to key the
        # later authoritative Daraja reconciliation, but we must not hold a
        # database lock while we perform the slow network round-trip.
        mpesa_transaction = (
            MpesaTransaction.query.filter_by(
                checkout_request_id=checkout_request_id
            ).first()
        )

        if not mpesa_transaction:
            current_app.logger.warning(
                "Received M-Pesa callback for unknown checkout_request_id=%s",
                checkout_request_id,
            )
            return None

        mpesa_transaction_id = mpesa_transaction.id

        # Release the read-only transaction immediately so no lock or open
        # snapshot is held across the outbound Daraja call below.
        db.session.rollback()

        current_app.logger.info(
            "MPESA_EVENT=CALLBACK_RECEIVED mpesa_transaction=%s "
            "checkout_request_id=%s merchant_request_id=%s callback_result_code=%s",
            mpesa_transaction_id,
            checkout_request_id,
            parsed_callback.get("merchant_request_id"),
            parsed_callback.get("result_code"),
        )

        # Phase 2 — reconcile with Daraja (authenticated server-to-server). NO
        # database transaction or row lock is held during this network call.
        # The query is deduplicated per checkout_request_id and funnelled through
        # the shared global rate limiter / upstream cooldown, but it deliberately
        # does NOT apply the per-transaction reconciliation backoff: a callback is
        # an external event and must be handled promptly.
        query_result, performed_query, throttled = MpesaService._callback_query(
            checkout_request_id
        )

        if not performed_query:
            # Another in-flight callback is already reconciling this exact
            # checkout request. Re-read the latest state under a lock and return
            # it without issuing a second Daraja query (no duplicate query, no
            # credit). The dedup guard guarantees at most one Daraja query per
            # checkout request at any instant.
            current_app.logger.info(
                "MPESA_EVENT=CALLBACK_DEDUP_SKIP mpesa_transaction=%s "
                "checkout_request_id=%s",
                mpesa_transaction_id,
                checkout_request_id,
            )
            locked = MpesaService._lock_mpesa_transaction(mpesa_transaction_id)
            if locked is None:
                db.session.rollback()
                return None
            db.session.rollback()
            return locked

        if throttled:
            # Global upstream cooldown / rate limit: do NOT credit, do NOT mark
            # failed, and do NOT charge a per-transaction attempt. Keep recoverable.
            current_app.logger.error(
                "MPESA_EVENT=CALLBACK_THROTTLED mpesa_transaction=%s "
                "checkout_request_id=%s",
                mpesa_transaction_id,
                checkout_request_id,
            )
            return MpesaService._callback_keep_recoverable(
                mpesa_transaction_id, parsed_callback, checkout_request_id,
                record_attempt=False,
            )

        if query_result is None:
            # Daraja could not be reached (network/timeout): do NOT credit and do
            # NOT mark the deposit failed. Acquire the row lock, re-read the latest
            # state, and only then keep the deposit recoverable — a concurrent
            # callback/sweeper may already have credited it.
            return MpesaService._callback_keep_recoverable(
                mpesa_transaction_id, parsed_callback, checkout_request_id
            )

        # Phase 3 — acquire the row lock and re-read the authoritative latest
        # state. A concurrent callback/sweeper may have resolved this deposit
        # while the Daraja query was in flight.
        mpesa_transaction = MpesaService._lock_mpesa_transaction(mpesa_transaction_id)

        if mpesa_transaction is None:
            db.session.rollback()
            return None

        # Idempotency guard: terminal states are never re-credited or flipped.
        if MpesaTransactionStatus.is_terminal(mpesa_transaction.status):
            current_app.logger.info(
                "Ignoring duplicate M-Pesa callback: mpesa_transaction=%s status=%s",
                mpesa_transaction.id,
                mpesa_transaction.status,
            )
            db.session.rollback()
            return mpesa_transaction

        # Record the callback envelope (attacker-controlled; never trusted for a
        # credit) now that we hold the lock and have re-read the row, so a caller
        # holding a stale copy cannot clobber another worker's writes.
        mpesa_transaction.result_code = truncate(parsed_callback["result_code"], 10)
        mpesa_transaction.result_desc = truncate(
            parsed_callback["result_desc"], RESULT_DESC_MAX_LENGTH
        )
        mpesa_transaction.transaction_date = truncate(
            parsed_callback["transaction_date"], 20
        )

        query_code = str(query_result.get("ResultCode"))

        if query_code == "0":
            current_app.logger.info(
                "MPESA_EVENT=CALLBACK_SUCCESS mpesa_transaction=%s "
                "checkout_request_id=%s query_result_code=%s",
                mpesa_transaction.id,
                checkout_request_id,
                query_code,
            )
            return MpesaService._credit_confirmed_deposit(
                mpesa_transaction,
                callback_amount=parsed_callback["amount"],
                receipt_number=parsed_callback["mpesa_receipt_number"],
            )

        if query_code in DEFINITIVE_FAILURE_RESULT_CODES:
            mpesa_transaction.status = MpesaTransactionStatus.FAILED
            mpesa_transaction.failure_reason = truncate(
                query_result.get("ResultDesc"), RESULT_DESC_MAX_LENGTH
            )
            if not mpesa_transaction.result_desc:
                mpesa_transaction.result_desc = truncate(
                    query_result.get("ResultDesc"), RESULT_DESC_MAX_LENGTH
                )
            MpesaService._record_reconciliation_attempt(
                mpesa_transaction, query_result
            )
            db.session.commit()
            current_app.logger.info(
                "MPESA_EVENT=CALLBACK_FAILURE mpesa_transaction=%s code=%s "
                "checkout_request_id=%s",
                mpesa_transaction.id,
                query_code,
                checkout_request_id,
            )
            return mpesa_transaction

        # Inconclusive / unrecognised non-zero result: keep the deposit
        # recoverable. NEVER treat this as proof the customer did not pay.
        mpesa_transaction.status = MpesaTransactionStatus.RECONCILIATION_PENDING
        MpesaService._record_reconciliation_attempt(
            mpesa_transaction, query_result
        )
        db.session.commit()
        current_app.logger.info(
            "MPESA_EVENT=RECONCILIATION_RESULT mpesa_transaction=%s outcome="
            "INCONCLUSIVE query_result_code=%s checkout_request_id=%s status=%s",
            mpesa_transaction.id,
            query_code,
            checkout_request_id,
            mpesa_transaction.status,
        )
        return mpesa_transaction

    @staticmethod
    def _callback_keep_recoverable(
        mpesa_transaction_id, parsed_callback, checkout_request_id, record_attempt=True
    ):
        """Daraja unreachable from the callback: keep the deposit recoverable.

        Re-acquires the row lock, re-reads the latest state, and only then marks
        the deposit ``RECONCILIATION_PENDING``. A concurrent callback/sweeper may
        already have credited or failed the deposit, in which case we leave it
        untouched rather than clobbering its terminal state.

        ``record_attempt`` is ``False`` when the callback was blocked by the global
        upstream cooldown / rate limiter: an upstream-wide incident must never
        charge a per-transaction reconciliation attempt.
        """
        locked = MpesaService._lock_mpesa_transaction(mpesa_transaction_id)

        if locked is None:
            db.session.rollback()
            return None

        if MpesaTransactionStatus.is_terminal(locked.status):
            db.session.rollback()
            return locked

        # Record the callback envelope (attacker-controlled; never trusted) while
        # we hold the lock, then keep the deposit recoverable for a later retry.
        locked.result_code = truncate(parsed_callback["result_code"], 10)
        locked.result_desc = truncate(
            parsed_callback["result_desc"], RESULT_DESC_MAX_LENGTH
        )
        locked.transaction_date = truncate(
            parsed_callback["transaction_date"], 20
        )
        locked.status = MpesaTransactionStatus.RECONCILIATION_PENDING
        if record_attempt:
            MpesaService._record_reconciliation_attempt(locked)
        else:
            # Still stamp the last attempt time for observability without bumping
            # the attempt counter.
            locked.last_reconciled_at = datetime.utcnow()
        db.session.commit()
        current_app.logger.error(
            "M-Pesa reconciliation unreachable; keeping deposit recoverable: "
            "mpesa_transaction=%s record_attempt=%s",
            locked.id,
            record_attempt,
        )
        return locked

    @staticmethod
    def _callback_query(checkout_request_id):
        """Query Daraja for a callback, deduplicated per checkout_request_id.

        Returns ``(result, performed, throttled)``:
        * ``performed`` is ``False`` when another in-flight callback already owns
          the query for this checkout id (the caller should re-read state and
          skip, not credit);
        * ``throttled`` is ``True`` when the global limiter / upstream cooldown
          blocked the request (the caller must keep the deposit recoverable
          without charging a per-transaction attempt);
        * ``result`` is ``None`` when no authoritative query result is available
          (network/timeout/throttled).
        """
        with _CALLBACK_DEDUP_GUARD:
            event = _callback_inflight.get(checkout_request_id)
            if event is not None and not event.is_set():
                owned = False
            else:
                event = threading.Event()
                _callback_inflight[checkout_request_id] = event
                owned = True

        if not owned:
            # Another callback is mid-query for this checkout id: wait for it
            # (bounded) then signal the caller to reuse the resolved state.
            try:
                timeout = float(current_app.config.get("DARAJA_TIMEOUT", 30)) + 10
            except (TypeError, ValueError):
                timeout = 40.0
            event.wait(timeout=timeout)
            return None, False, False

        try:
            try:
                result = MpesaService.query_stk_status(checkout_request_id)
                return result, True, False
            except (DarajaThrottled, DarajaUpstreamCooldown):
                return None, True, True
            except ApiError:
                return None, True, False
        finally:
            with _CALLBACK_DEDUP_GUARD:
                event.set()
                if _callback_inflight.get(checkout_request_id) is event:
                    _callback_inflight.pop(checkout_request_id, None)

    @staticmethod
    def _credit_confirmed_deposit(mpesa_transaction, callback_amount, receipt_number):
        """Credit the wallet for a deposit Daraja has confirmed as paid.

        This is the single crediting path, and it defends itself rather than
        trusting its caller:

        1. the deposit row is re-read under a row lock, so a caller holding a
           stale copy cannot act on an outcome another worker already applied;
        2. a terminal deposit (``Completed``/``Failed``) is refused outright and
           the unit of work is rolled back, so it can never be re-credited or
           flipped;
        3. the ledger entry is keyed on the canonical ``checkout_request_id``
           (identical for the callback and the recovery path), so the
           ``unique_wallet_ledger_reference`` constraint is a real database-level
           backstop instead of two paths that happen to disagree;
        4. wallet balance, ledger entry, internal transaction and deposit status
           are committed in one database transaction, or none of them are.

        The credited amount is always the stored requested amount, never the
        callback's reported amount, so a forged amount cannot inflate a balance.
        A mismatching callback amount blocks the credit but never marks the
        deposit ``FAILED``: Daraja already confirmed the payment, so the deposit
        stays recoverable.
        """
        # Defence 1/2: lock and re-read before deciding anything.
        locked = MpesaService._lock_mpesa_transaction(mpesa_transaction.id)

        if locked is not None:
            mpesa_transaction = locked

        if MpesaTransactionStatus.is_terminal(mpesa_transaction.status):
            # Roll back so nothing staged by the caller can reach the database.
            db.session.rollback()
            current_app.logger.info(
                "Refusing to credit an already terminal M-Pesa deposit: "
                "mpesa_transaction=%s status=%s",
                mpesa_transaction.id,
                mpesa_transaction.status,
            )
            return mpesa_transaction

        expected_amount = to_money(mpesa_transaction.amount)
        checkout_request_id = mpesa_transaction.checkout_request_id
        receipt_number = truncate(receipt_number, 32)

        if callback_amount is not None:
            try:
                callback_amount = to_money(callback_amount)
            except (ArithmeticError, ValueError):
                callback_amount = None

            if callback_amount is not None and callback_amount != expected_amount:
                # Daraja confirmed the payment, so a disagreeing callback amount
                # is NOT evidence of non-payment. Never mark it FAILED; keep it
                # recoverable and uncredited until the amount is confirmed.
                mpesa_transaction.status = (
                    MpesaTransactionStatus.RECONCILIATION_PENDING
                )
                mpesa_transaction.failure_reason = truncate(
                    AMOUNT_MISMATCH_REASON, RESULT_DESC_MAX_LENGTH
                )
                MpesaService._record_reconciliation_attempt(mpesa_transaction)
                db.session.commit()
                current_app.logger.error(
                    "M-Pesa callback amount mismatch; deposit left uncredited and "
                    "recoverable: mpesa_transaction=%s",
                    mpesa_transaction.id,
                )
                return mpesa_transaction

        # Defence 3: one canonical idempotency reference per deposit, shared by
        # the callback and the recovery path. ``checkout_request_id`` is issued
        # by Daraja, is unique on ``mpesa_transactions`` and is known on both
        # paths, which makes it the only reference that lets
        # ``unique_wallet_ledger_reference`` block a duplicate credit.
        reference = checkout_request_id or receipt_number

        if not reference:
            mpesa_transaction.status = MpesaTransactionStatus.RECONCILIATION_PENDING
            mpesa_transaction.failure_reason = truncate(
                MISSING_REFERENCE_REASON, RESULT_DESC_MAX_LENGTH
            )
            MpesaService._record_reconciliation_attempt(mpesa_transaction)
            db.session.commit()
            current_app.logger.error(
                "M-Pesa deposit has no idempotency reference; left uncredited: "
                "mpesa_transaction=%s",
                mpesa_transaction.id,
            )
            return mpesa_transaction

        try:
            wallet = WalletService.get_locked_wallet(mpesa_transaction.user_id)

            transaction = TransactionService.record_deposit(
                user=mpesa_transaction.user,
                wallet=wallet,
                amount=expected_amount,
                reference=reference,
                description="M-Pesa deposit",
            )

            current_app.logger.info(
                "MPESA_EVENT=DEPOSIT_RECORDED mpesa_transaction=%s transaction=%s "
                "amount=%s reference=%s",
                mpesa_transaction.id,
                transaction.id,
                expected_amount,
                reference,
            )

            if receipt_number:
                # Only ever set the receipt; never overwrite a stored one with
                # ``None`` (the recovery path has no receipt to report).
                mpesa_transaction.mpesa_receipt_number = receipt_number

            mpesa_transaction.status = MpesaTransactionStatus.COMPLETED
            mpesa_transaction.transaction = transaction

            db.session.commit()

            current_app.logger.info(
                "MPESA_EVENT=WALLET_CREDITED mpesa_transaction=%s "
                "checkout_request_id=%s user=%s wallet=%s amount=%s "
                "transaction=%s",
                mpesa_transaction.id,
                checkout_request_id,
                mpesa_transaction.user_id,
                wallet.id,
                expected_amount,
                transaction.id,
            )
            current_app.logger.info(
                "MPESA_EVENT=LEDGER_ENTRY_CREATED mpesa_transaction=%s "
                "wallet=%s reference=%s",
                mpesa_transaction.id,
                wallet.id,
                reference,
            )

        except IntegrityError:
            # The database-level backstop fired: this payment is already
            # credited. Never retry the credit.
            db.session.rollback()
            current_app.logger.warning(
                "MPESA_EVENT=DUPLICATE_CREDIT_PREVENTED "
                "checkout_request_id=%s",
                checkout_request_id,
            )
            return MpesaTransaction.query.filter_by(
                checkout_request_id=checkout_request_id
            ).first()

        except ApiError:
            db.session.rollback()
            raise

        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Could not credit M-Pesa deposit")
            raise ApiError(
                "Deposit could not be processed.",
                500,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        return mpesa_transaction

    @staticmethod
    def _recoverable_candidates():
        """Snapshot the recoverable deposits as ``(id, checkout_request_id)``.

        Only identifiers are read, and the read transaction is released before
        returning, so no database lock or open transaction is held across the
        outbound Daraja calls that follow. The snapshot is deliberately treated
        as advisory: every row is re-read under a lock before anything is
        written, because a callback may resolve it in the meantime.
        """
        rows = (
            db.session.query(
                MpesaTransaction.id, MpesaTransaction.checkout_request_id
            )
            .filter(
                MpesaTransaction.status.in_(
                    MpesaTransactionStatus.RECOVERABLE_STATUSES
                )
            )
            .order_by(MpesaTransaction.id)
            .all()
        )

        # Release the read transaction; nothing must be held during HTTP.
        db.session.rollback()

        return [(row[0], row[1]) for row in rows]

    @staticmethod
    def _recover_one(mpesa_transaction_id, checkout_request_id):
        """Reconcile one deposit and return the summary key for its outcome.

        Ordering matters and is the fix for the callback/recovery double-credit
        race: Daraja is queried *before* any lock is taken, and the outcome is
        applied *after* the deposit row has been locked and its status re-read.
        A deposit that a concurrent callback completed while this query was in
        flight is therefore skipped rather than credited a second time.
        """
        config = current_app.config

        if not checkout_request_id:
            # Nothing to reconcile against Daraja; leave it recoverable.
            current_app.logger.warning(
                "MPESA_EVENT=RECONCILIATION_SKIPPED mpesa_transaction=%s "
                "reason=missing_checkout_request_id",
                mpesa_transaction_id,
            )
            return "reconciliation_pending"

        # No database lock and no open transaction is held for this call.
        current_app.logger.info(
            "MPESA_EVENT=RECONCILIATION_STARTED mpesa_transaction=%s "
            "checkout_request_id=%s",
            mpesa_transaction_id,
            checkout_request_id,
        )

        # --- per-transaction reconciliation metadata read (timezone-safe) ---
        try:
            meta = (
                MpesaTransaction.query.with_entities(
                    MpesaTransaction.reconciliation_attempts,
                    MpesaTransaction.last_reconciled_at,
                    MpesaTransaction.created_at,
                )
                .filter_by(id=mpesa_transaction_id)
                .first()
            )
        except Exception:
            meta = None
        # Close the read transaction; nothing must be held during the HTTP call.
        db.session.rollback()

        attempts = (meta[0] if meta is not None and meta[0] is not None else 0)
        last = meta[1] if meta is not None else None
        created = meta[2] if meta is not None else None

        # --- max attempts -> manual review (terminal hold, no auto reconcile) -
        max_attempts = config.get("MPESA_MAX_RECONCILIATION_ATTEMPTS")
        if max_attempts is not None and attempts >= int(max_attempts):
            return MpesaService._transition_to_manual_review(
                mpesa_transaction_id,
                reason=(
                    "Automatic reconciliation exhausted after "
                    f"{attempts} attempts; requires manual review."
                ),
            )

        # --- max age -> manual review (stop retrying a deposit stuck for days) -
        # A deposit that has been recoverable for far longer than any genuine
        # STK Push could still be pending is held for a human instead of being
        # re-queried forever (see STUCK_DEPOSIT_ALERT). ``created_at`` is stored
        # as naive UTC, matching ``datetime.utcnow()``.
        max_age = config.get("MPESA_MAX_RECONCILIATION_AGE_SECONDS")
        try:
            max_age = int(max_age) if max_age is not None else 0
        except (TypeError, ValueError):
            max_age = 0
        if max_age > 0 and created is not None:
            age = (datetime.utcnow() - created).total_seconds()
            if age >= max_age:
                current_app.logger.error(
                    "MPESA_EVENT=RECONCILIATION_MAX_AGE mpesa_transaction=%s "
                    "age_seconds=%s attempts=%s",
                    mpesa_transaction_id,
                    int(age),
                    attempts,
                )
                return MpesaService._transition_to_manual_review(
                    mpesa_transaction_id,
                    reason=(
                        f"Deposit unresolved for ~{int(age // 3600)}h "
                        f"after {attempts} reconciliation attempts; "
                        "requires manual review."
                    ),
                    enforce_attempt_budget=False,
                )

        # --- per-transaction backoff (genuine outcomes only, not 403/429) -----
        # ``last_reconciled_at`` is stored as naive UTC, so elapsed time uses
        # naive UTC arithmetic and is identical under UTC, Africa/Nairobi and any
        # other host timezone. 403/429 are handled by the global upstream
        # cooldown, never charged here.
        if last is not None:
            elapsed = (datetime.utcnow() - last).total_seconds()
            backoff = _mpesa_backoff_seconds(attempts, config)
            if elapsed < backoff:
                current_app.logger.info(
                    "MPESA_EVENT=RECONCILIATION_SKIPPED mpesa_transaction=%s "
                    "reason=backoff next_allowed_in=%s attempts=%s",
                    mpesa_transaction_id,
                    int(backoff - elapsed),
                    attempts,
                )
                return "reconciliation_pending"

        # --- namespaced per-transaction advisory lock + Daraja query ---------
        # Two-argument advisory lock so it can never collide with the sweeper
        # leader lock (which uses the single-argument form). The lock is held on
        # a dedicated AUTOCOMMIT connection and released in a single ``finally``
        # below, and is kept held until the reconciliation state update commits.
        tx_conn = None
        advisory_acquired = False
        try:
            if db.engine.dialect.name == "postgresql":
                tx_conn = db.engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                advisory_acquired = tx_conn.execute(
                    text(
                        "SELECT pg_try_advisory_lock("
                        "CAST(:k1 AS integer), CAST(:k2 AS integer))"
                    ),
                    {"k1": TX_ADVISORY_NAMESPACE, "k2": mpesa_transaction_id},
                ).scalar()
                if not advisory_acquired:
                    current_app.logger.info(
                        "MPESA_EVENT=RECONCILIATION_SKIPPED mpesa_transaction=%s "
                        "reason=lock_held_by_other_process",
                        mpesa_transaction_id,
                    )
                    return "skipped"

            # Daraja query — funnelled through the shared global rate limiter and
            # upstream cooldown inside _request_json. No DB transaction is open.
            try:
                query_result = MpesaService.query_stk_status(checkout_request_id)
            except DarajaTransactionUnknown as error:
                # Daraja says THIS checkout request does not exist / is expired.
                # Terminal for this transaction only (never a global cooldown):
                # stop polling it and hand it to a human.
                current_app.logger.error(
                    "MPESA_EVENT=RECONCILIATION_TRANSACTION_UNKNOWN "
                    "mpesa_transaction=%s checkout_request_id=%s error_code=%s",
                    mpesa_transaction_id,
                    checkout_request_id,
                    error.error_code,
                )
                return MpesaService._transition_to_manual_review(
                    mpesa_transaction_id,
                    reason=truncate(
                        "Daraja reports this checkout request does not exist "
                        f"(errorCode {error.error_code or 'unknown'}). "
                        "Automatic reconciliation stopped; requires manual "
                        "review.",
                        RESULT_DESC_MAX_LENGTH,
                    ),
                    enforce_attempt_budget=False,
                )
            except (DarajaThrottled, DarajaUpstreamCooldown):
                # Global upstream incident: do NOT credit, do NOT charge an
                # attempt, do NOT change the financial state. Keep recoverable.
                current_app.logger.error(
                    "MPESA_EVENT=RECONCILIATION_THROTTLED mpesa_transaction=%s "
                    "checkout_request_id=%s",
                    mpesa_transaction_id,
                    checkout_request_id,
                )
                return "throttled"
            except ApiError:
                # Network/timeout/etc.: keep recoverable (charges an attempt).
                query_result = None

            # Apply the outcome under a row lock. The advisory lock (tx_conn)
            # remains held until this state update commits (released in finally).
            return MpesaService._apply_reconciliation(
                mpesa_transaction_id, checkout_request_id, query_result
            )
        finally:
            if tx_conn is not None:
                try:
                    if advisory_acquired:
                        tx_conn.execute(
                            text(
                                "SELECT pg_advisory_unlock("
                                "CAST(:k1 AS integer), CAST(:k2 AS integer))"
                            ),
                            {"k1": TX_ADVISORY_NAMESPACE, "k2": mpesa_transaction_id},
                        )
                except Exception:
                    current_app.logger.exception(
                        "MPESA_EVENT=RECONCILIATION_UNLOCK_ERROR "
                        "mpesa_transaction=%s",
                        mpesa_transaction_id,
                    )
                finally:
                    try:
                        tx_conn.close()
                    except Exception:
                        pass

    @staticmethod
    def _apply_reconciliation(mpesa_transaction_id, checkout_request_id, query_result):
        """Apply a Daraja reconciliation outcome under a row lock.

        Called with the per-transaction advisory lock already held (and released
        by the caller). Returns the summary key for the outcome. This is the
        single place that turns a Daraja result into a state change/credit, so
        the financial-safety rules are enforced in exactly one spot:

        * ResultCode 0  -> credit (only automatic credit path);
        * 1032 (and other definitive failures) -> FAILED (no credit);
        * network/timeout (query_result is None) -> keep recoverable, charge an
          attempt (but this is NOT a 403/429 upstream incident);
        * anything else -> keep recoverable (never FAILED, never credited).
        """
        mpesa_transaction = MpesaService._lock_mpesa_transaction(
            mpesa_transaction_id
        )

        if mpesa_transaction is None:
            db.session.rollback()
            current_app.logger.warning(
                "MPESA_EVENT=RECONCILIATION_ERROR mpesa_transaction=%s "
                "reason=disappeared",
                mpesa_transaction_id,
            )
            return "skipped"

        # Re-check under the lock: a callback may have resolved this deposit
        # while the Daraja query was in flight.
        if MpesaTransactionStatus.is_terminal(mpesa_transaction.status):
            db.session.rollback()
            current_app.logger.info(
                "MPESA_EVENT=RECONCILIATION_SKIPPED mpesa_transaction=%s "
                "reason=already_terminal status=%s",
                mpesa_transaction.id,
                mpesa_transaction.status,
            )
            return "skipped"

        if query_result is None:
            # Daraja unreachable (network/timeout): keep it recoverable, never
            # failed, and charge an attempt so the per-transaction backoff can
            # space out genuine retries. (403/429 upstream incidents never reach
            # here — they return "throttled" earlier and charge no attempt.)
            mpesa_transaction.status = MpesaTransactionStatus.RECONCILIATION_PENDING
            MpesaService._record_reconciliation_attempt(mpesa_transaction)
            db.session.commit()
            current_app.logger.error(
                "MPESA_EVENT=RECONCILIATION_RESULT mpesa_transaction=%s "
                "outcome=UNREACHABLE checkout_request_id=%s",
                mpesa_transaction.id,
                checkout_request_id,
            )
            return "reconciliation_pending"

        query_code = str(query_result.get("ResultCode"))

        if query_code == "0":
            # Persist query metadata and credit in the same locked unit of work,
            # through the single credit path.
            MpesaService._record_reconciliation_attempt(
                mpesa_transaction, query_result
            )
            credited = MpesaService._credit_confirmed_deposit(
                mpesa_transaction,
                callback_amount=None,
                receipt_number=mpesa_transaction.mpesa_receipt_number,
            )

            if (
                credited is not None
                and credited.status == MpesaTransactionStatus.COMPLETED
            ):
                current_app.logger.info(
                    "MPESA_EVENT=RECONCILIATION_RESULT mpesa_transaction=%s "
                    "outcome=CREDITED checkout_request_id=%s",
                    mpesa_transaction.id,
                    checkout_request_id,
                )
                return "credited"

            # The credit was refused or rolled back (for example the database
            # backstop fired); report it rather than counting a phantom credit.
            current_app.logger.error(
                "MPESA_EVENT=RECONCILIATION_RESULT mpesa_transaction=%s "
                "outcome=CREDIT_ERROR checkout_request_id=%s",
                mpesa_transaction.id,
                checkout_request_id,
            )
            return "errors"

        if query_code in DEFINITIVE_FAILURE_RESULT_CODES:
            mpesa_transaction.status = MpesaTransactionStatus.FAILED
            mpesa_transaction.failure_reason = truncate(
                query_result.get("ResultDesc"), RESULT_DESC_MAX_LENGTH
            )
            MpesaService._record_reconciliation_attempt(
                mpesa_transaction, query_result
            )
            db.session.commit()
            current_app.logger.info(
                "MPESA_EVENT=RECONCILIATION_RESULT mpesa_transaction=%s "
                "outcome=FAILED checkout_request_id=%s code=%s",
                mpesa_transaction.id,
                checkout_request_id,
                query_code,
            )
            return "failed"

        # Inconclusive / unrecognised non-zero result: keep recoverable.
        mpesa_transaction.status = MpesaTransactionStatus.RECONCILIATION_PENDING
        MpesaService._record_reconciliation_attempt(mpesa_transaction, query_result)
        db.session.commit()
        current_app.logger.info(
            "MPESA_EVENT=RECONCILIATION_RESULT mpesa_transaction=%s "
            "outcome=INCONCLUSIVE checkout_request_id=%s code=%s",
            mpesa_transaction.id,
            checkout_request_id,
            query_code,
        )
        return "reconciliation_pending"

    @staticmethod
    def _transition_to_manual_review(
        mpesa_transaction_id, reason=None, enforce_attempt_budget=True
    ):
        """Move a deposit into the terminal ``ManualReviewRequired`` hold.

        Used when automatic reconciliation can no longer make progress:

        * it exhausted its attempt budget
          (``MPESA_MAX_RECONCILIATION_ATTEMPTS``), or
        * it exceeded its age budget
          (``MPESA_MAX_RECONCILIATION_AGE_SECONDS``), or
        * Daraja reported the checkout request as unknown/expired
          (``DarajaTransactionUnknown``, errorCode ``500.001.1001``).

        The deposit is then excluded from every automatic recovery path
        (sweeper/user/admin/callback), remains uncredited, and is never
        automatically marked FAILED — a human must resolve it. The existing
        idempotent credit path remains available for a future human-confirmed
        payment, so this is a hold, not a loss.

        ``enforce_attempt_budget`` guards only the attempt-exhaustion caller:
        it re-checks under the lock that a concurrent path has not dropped the
        attempt count back below the threshold. Age- and unknown-transaction
        transitions pass ``False`` because their trigger is independent of the
        attempt count.
        """
        config = current_app.config
        max_attempts = config.get("MPESA_MAX_RECONCILIATION_ATTEMPTS")
        locked = MpesaService._lock_mpesa_transaction(mpesa_transaction_id)

        if locked is None:
            db.session.rollback()
            return "skipped"

        if MpesaTransactionStatus.is_terminal(locked.status):
            db.session.rollback()
            return "skipped"

        if (
            enforce_attempt_budget
            and max_attempts is not None
            and (locked.reconciliation_attempts or 0) < int(max_attempts)
        ):
            # A concurrent path already advanced it below the threshold; let the
            # normal reconciliation path handle it instead of forcing a hold.
            db.session.rollback()
            return "reconciliation_pending"

        locked.status = MpesaTransactionStatus.MANUAL_REVIEW_REQUIRED
        locked.failure_reason = truncate(
            reason
            or "Exhausted automatic reconciliation attempts; requires manual "
            "review.",
            RESULT_DESC_MAX_LENGTH,
        )
        db.session.commit()
        current_app.logger.error(
            "MPESA_EVENT=MANUAL_REVIEW_REQUIRED mpesa_transaction=%s attempts=%s "
            "reason=%s",
            locked.id,
            locked.reconciliation_attempts or 0,
            locked.failure_reason,
        )
        return "manual_review"

    @staticmethod
    def recover_deposits():
        """Recover PENDING and RECONCILIATION_PENDING deposits via Daraja.

        This is the R2 recovery path for deposits whose callback never arrived,
        or whose earlier reconciliation was inconclusive. Every eligible row is
        re-checked against Daraja (server-to-server, authenticated) and:

        * confirmed (ResultCode 0) -> credited exactly once, COMPLETED;
        * a recognised definitive failure (e.g. 1032) -> FAILED (no credit);
        * inconclusive / unrecognised / error -> RECONCILIATION_PENDING (no
          credit, recovery path preserved);
        * already resolved by a concurrent callback -> skipped (no credit).

        Each deposit is processed in its own database transaction, so one bad
        row is rolled back and counted in ``errors`` without aborting the sweep
        or undoing rows that already committed.

        Returns a safe summary count dict.
        """
        summary = {
            "processed": 0,
            "credited": 0,
            "failed": 0,
            "reconciliation_pending": 0,
            "throttled": 0,
            "manual_review": 0,
            "skipped": 0,
            "errors": 0,
        }

        for mpesa_transaction_id, checkout_request_id in (
            MpesaService._recoverable_candidates()
        ):
            summary["processed"] += 1

            try:
                outcome = MpesaService._recover_one(
                    mpesa_transaction_id, checkout_request_id
                )
            except Exception:
                # Isolate the failure: roll back only this row's work and carry
                # on with the remaining deposits.
                db.session.rollback()
                current_app.logger.exception(
                    "M-Pesa recovery failed for mpesa_transaction=%s",
                    mpesa_transaction_id,
                )
                summary["errors"] += 1
                continue

            summary[outcome] += 1

        return summary

    @staticmethod
    def reconcile_pending():
        """Backwards-compatible alias for :meth:`recover_deposits`."""
        return MpesaService.recover_deposits()

    @staticmethod
    def reconcile_user_deposit(user_id, transaction_id):
        """User-scoped reconciliation for a single deposit.

        Ownership-checked wrapper around :meth:`_recover_one` so the frontend
        can nudge recovery of the caller's own stuck deposit (for example when a
        callback never arrived, or arrived while Daraja's live query was still
        inconclusive) without waiting for the background sweep or an admin.

        Returns the resulting transaction status string. Never credits a wallet
        that is not the caller's, and raises ``404`` when the deposit does not
        belong to the user so its existence is not leaked. The operation is
        idempotent and safe to call repeatedly: a terminal deposit is skipped and
        a confirmed-but-already-credited one is blocked by the ledger constraint.
        """
        mpesa_transaction = MpesaTransaction.query.filter_by(
            id=transaction_id, user_id=user_id
        ).first()

        if not mpesa_transaction:
            raise ApiError(
                "M-Pesa transaction not found.",
                404,
                ErrorCode.TRANSACTION_NOT_FOUND,
            )

        checkout_request_id = mpesa_transaction.checkout_request_id

        if not checkout_request_id:
            # No Daraja reference to reconcile against; only a later callback or
            # an admin action can resolve it. Leave the current status untouched.
            current_app.logger.info(
                "MPESA_EVENT=USER_RECONCILE_SKIPPED mpesa_transaction=%s "
                "reason=missing_checkout_request_id",
                mpesa_transaction.id,
            )
            return mpesa_transaction.status

        current_app.logger.info(
            "MPESA_EVENT=USER_RECONCILE_STARTED mpesa_transaction=%s "
            "checkout_request_id=%s",
            mpesa_transaction.id,
            checkout_request_id,
        )

        outcome = MpesaService._recover_one(
            mpesa_transaction.id, checkout_request_id
        )

        current_app.logger.info(
            "MPESA_EVENT=USER_RECONCILE_RESULT mpesa_transaction=%s outcome=%s",
            mpesa_transaction.id,
            outcome,
        )

        refreshed = MpesaTransaction.query.filter_by(
            id=mpesa_transaction.id
        ).first()

        return refreshed.status if refreshed else mpesa_transaction.status

    # --- sweeper leadership (cross-process coordination) -------------------

    # Stable, application-specific advisory-lock key so exactly one process per
    # database becomes the reconciliation leader. Distinct from any other
    # advisory lock the application might use.
    DEFAULT_SWEEPER_LEADER_LOCK_ID = 912374561

    # Session-level advisory lock statements.
    #
    # These MUST be run through ``Connection.execute(text(...))`` with bound
    # parameters and NOT through ``Connection.exec_driver_sql()``:
    # ``exec_driver_sql()`` deliberately bypasses SQLAlchemy's SQL compilation
    # and passes the string straight to the DBAPI, so a SQLAlchemy-style
    # ``:key`` bind is never translated into psycopg2's ``pyformat``
    # (``%(key)s``) paramstyle. PostgreSQL then receives the literal text
    # ``pg_try_advisory_lock(:key)`` and fails with
    # ``syntax error at or near ":"``. ``text()`` compiles the named bind into
    # whatever paramstyle the driver uses, so the lock id is sent as a real
    # bound parameter.
    #
    # The explicit ``CAST(... AS bigint)`` pins PostgreSQL's function
    # resolution to the single-argument ``pg_try_advisory_lock(bigint)``
    # overload no matter how the driver types the parameter.
    _ACQUIRE_SWEEPER_LOCK_SQL = text(
        "SELECT pg_try_advisory_lock(CAST(:key AS bigint))"
    )
    _RELEASE_SWEEPER_LOCK_SQL = text(
        "SELECT pg_advisory_unlock(CAST(:key AS bigint))"
    )

    @staticmethod
    def _sweeper_leader_lock_id(app):
        try:
            return int(
                app.config.get(
                    "MPESA_SWEEPER_LEADER_LOCK_ID",
                    MpesaService.DEFAULT_SWEEPER_LEADER_LOCK_ID,
                )
            )
        except (TypeError, ValueError):
            return MpesaService.DEFAULT_SWEEPER_LEADER_LOCK_ID

    @staticmethod
    def _acquire_sweeper_leadership(app, leader_state):
        """Return ``True`` if this process may run the sweep this cycle.

        On PostgreSQL a session-level advisory lock guarantees that, across all
        application processes/workers/instances sharing the database, exactly one
        becomes the reconciliation leader and the rest skip their sweeper cycles
        instead of duplicating Daraja calls and database contention. The lock is
        acquired for the duration of a single cycle and released afterwards, so a
        leader that dies (its connection is closed by the server) automatically
        releases the lock and another process takes over on its next cycle.

        On non-PostgreSQL backends (e.g. SQLite in development) advisory locks
        are unavailable, so cross-process coordination is impossible. We run the
        sweeper directly and warn loudly: this is only safe for a single process
        and MUST NOT be assumed safe for multi-instance production. No fake
        locking is performed.
        """
        dialect = db.engine.dialect.name
        if dialect != "postgresql":
            if not leader_state.get("warned_non_pg"):
                app.logger.warning(
                    "MPESA_EVENT=SWEEPER_LEADERSHIP_UNSUPPORTED dialect=%s "
                    "reason=advisory_locks_unavailable "
                    "multiple_instances_would_duplicate_sweeps",
                    dialect,
                )
                leader_state["warned_non_pg"] = True
            return True

        conn = None
        try:
            # The leadership connection is held for the whole cycle. Session
            # level advisory locks are independent of transaction state, so
            # AUTOCOMMIT keeps the connection from sitting idle-in-transaction
            # for the duration of the sweep while the lock semantics stay
            # exactly the same (only pg_advisory_unlock or the end of the
            # session releases it).
            conn = db.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            )
            acquired = conn.execute(
                MpesaService._ACQUIRE_SWEEPER_LOCK_SQL,
                {"key": MpesaService._sweeper_leader_lock_id(app)},
            ).scalar()
        except Exception:
            app.logger.exception("MPESA_EVENT=SWEEPER_LEADERSHIP_ERROR")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return False

        if not acquired:
            conn.close()
            if not leader_state.get("logged_not_leader"):
                app.logger.info(
                    "MPESA_EVENT=SWEEPER_NOT_LEADER lock_id=%s "
                    "reason=another_process_holds_leadership",
                    MpesaService._sweeper_leader_lock_id(app),
                )
                leader_state["logged_not_leader"] = True
            return False

        leader_state["conn"] = conn
        leader_state["logged_not_leader"] = False
        app.logger.info(
            "MPESA_EVENT=SWEEPER_LEADER_ACQUIRED lock_id=%s",
            MpesaService._sweeper_leader_lock_id(app),
        )
        return True

    @staticmethod
    def _release_sweeper_leadership(app, leader_state):
        """Release the per-cycle advisory leadership lock, if held."""
        conn = leader_state.pop("conn", None)
        if conn is None:
            return
        try:
            conn.execute(
                MpesaService._RELEASE_SWEEPER_LOCK_SQL,
                {"key": MpesaService._sweeper_leader_lock_id(app)},
            )
        except Exception:
            app.logger.exception("MPESA_EVENT=SWEEPER_LEADERSHIP_RELEASE_ERROR")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _alert_stuck_deposits(app):
        """Surface long-stuck recoverable deposits for operator attention.

        A deposit must never silently remain ``PENDING`` /
        ``RECONCILIATION_PENDING`` for days. This does NOT change the deposit's
        state or mark it ``FAILED`` — it only emits a structured warning carrying
        the transaction id and recovery metadata so a human can investigate. The
        thresholds are configurable via ``MPESA_STUCK_DEPOSIT_ALERT_SECONDS`` and
        ``MPESA_MAX_RECONCILIATION_ATTEMPTS``.
        """
        threshold = app.config.get("MPESA_STUCK_DEPOSIT_ALERT_SECONDS")
        max_attempts = app.config.get("MPESA_MAX_RECONCILIATION_ATTEMPTS")
        if threshold is None and max_attempts is None:
            return

        now = datetime.utcnow()
        rows = (
            MpesaTransaction.query.filter(
                MpesaTransaction.status.in_(
                    MpesaTransactionStatus.RECOVERABLE_STATUSES
                )
            ).all()
        )

        for row in rows:
            age = (
                (now - row.created_at).total_seconds()
                if row.created_at is not None
                else None
            )
            stuck_age = (
                age is not None
                and threshold is not None
                and age > threshold
            )
            stuck_attempts = (
                max_attempts is not None
                and (row.reconciliation_attempts or 0) >= max_attempts
            )
            if stuck_age or stuck_attempts:
                app.logger.warning(
                    "MPESA_EVENT=STUCK_DEPOSIT_ALERT mpesa_transaction=%s "
                    "status=%s age_seconds=%s reconciliation_attempts=%s "
                    "last_reconciled_at=%s action=manual_review_required",
                    row.id,
                    row.status,
                    int(age) if age is not None else None,
                    row.reconciliation_attempts or 0,
                    row.last_reconciled_at.isoformat()
                    if row.last_reconciled_at is not None
                    else None,
                )

    @staticmethod
    def start_reconciliation_sweeper(app, interval=None):
        """Start a background sweep that recovers stuck M-Pesa deposits.

        This closes the gap that caused the intermittent "paid but not credited"
        bug: a deposit left in ``PENDING`` (callback never reached the backend)
        or ``RECONCILIATION_PENDING`` (callback arrived while Daraja's live
        query was still inconclusive) was previously only resolved by a manual
        admin ``/admin/reconcile``. The sweeper periodically runs
        :meth:`recover_deposits`, so a genuine payment is always credited
        eventually, even if no callback and no frontend poll ever happens.

        Design notes / safety:
        * Not started when ``TESTING`` is set, so the test suite never performs
          real Daraja calls or spawns threads.
        * Runs in a daemon thread; one sweeper thread per process (guarded by a
          flag on the app object) so Flask's reloader does not multiply it.
        * Cross-process de-duplication is handled by PostgreSQL advisory-lock
          leader election (see :meth:`_acquire_sweeper_leadership`): even with
          multiple Gunicorn workers or multiple deployed instances, only one
          process runs the actual reconciliation cycles. On non-PostgreSQL
          backends the sweeper still runs but cannot coordinate, which is only
          safe for a single process.
        * The loop is observable (``SWEEPER_*`` events) and self-healing: any
          exception in a cycle is logged via ``SWEEPER_ERROR`` and the loop
          continues, so a single bad deposit or transient error can never stop
          recovery for the others.
        """
        if app.config.get("TESTING"):
            return

        try:
            interval = int(
                interval
                or app.config.get("MPESA_RECONCILIATION_INTERVAL_SECONDS", 60)
            )
        except (TypeError, ValueError):
            interval = 60

        if interval <= 0:
            return

        if getattr(app, "_mpesa_sweeper_started", False):
            return

        app._mpesa_sweeper_started = True

        def _run_sweeper():
            leader_state = {}
            try:
                while True:
                    time.sleep(interval)
                    try:
                        with app.app_context():
                            MpesaService._sweeper_cycle(app, leader_state)
                    except Exception:
                        # A cycle must never terminate the sweeper loop.
                        app.logger.exception("MPESA_EVENT=SWEEPER_ERROR")
            finally:
                # Release any held leadership lock on thread/process shutdown.
                MpesaService._release_sweeper_leadership(app, leader_state)

        sweeper_thread = threading.Thread(
            target=_run_sweeper,
            name="mpesa-reconciliation-sweeper",
            daemon=True,
        )
        sweeper_thread.start()
        app.logger.info(
            "MPESA_EVENT=SWEEPER_STARTED interval_seconds=%s", interval
        )

    @staticmethod
    def _sweeper_cycle(app, leader_state):
        """Run one reconciliation cycle if this process is the leader."""
        if not MpesaService._acquire_sweeper_leadership(app, leader_state):
            # Not the leader: do not duplicate Daraja calls or DB contention.
            return

        try:
            app.logger.info("MPESA_EVENT=SWEEPER_CYCLE_STARTED")
            summary = MpesaService.recover_deposits()
            MpesaService._alert_stuck_deposits(app)
            app.logger.info("MPESA_EVENT=SWEEPER_SUMMARY %s", summary)
        finally:
            MpesaService._release_sweeper_leadership(app, leader_state)
