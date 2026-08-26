"""Safaricom Daraja (M-Pesa) integration.

Only the approved MVP flow is implemented: an STK Push deposit that credits the
user's Vyloc wallet after Safaricom confirms the payment.

Credentials are read from configuration/environment variables, are never
hardcoded, and are never logged or returned in an API response.
"""

import base64
from datetime import datetime

import requests
from flask import current_app
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.mpesa_transaction import MpesaTransaction, MpesaTransactionStatus
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService
from app.utils.errors import ApiError, ErrorCode
from app.utils.helpers import generate_account_reference, to_money, truncate

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

    # --- Daraja calls --------------------------------------------------

    @staticmethod
    def get_access_token():
        """Request a Daraja OAuth access token."""
        config = MpesaService._config()

        try:
            response = requests.get(
                f"{config['DARAJA_BASE_URL']}{TOKEN_PATH}",
                auth=(
                    config["DARAJA_CONSUMER_KEY"],
                    config["DARAJA_CONSUMER_SECRET"],
                ),
                timeout=config["DARAJA_TIMEOUT"],
            )
            response.raise_for_status()
            token = (response.json() or {}).get("access_token")
        except (requests.RequestException, ValueError) as error:
            current_app.logger.error(
                "Daraja access token request failed: %s", type(error).__name__
            )
            raise ApiError(
                "Could not reach M-Pesa. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        if not token:
            current_app.logger.error("Daraja access token response had no token")
            raise ApiError(
                "Could not reach M-Pesa. Please try again.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        return token

    @staticmethod
    def send_stk_push(amount, phone, account_reference):
        """Send the STK Push request and return the Daraja response payload."""
        config = MpesaService._config()

        access_token = MpesaService.get_access_token()
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

        try:
            response = requests.post(
                f"{config['DARAJA_BASE_URL']}{STK_PUSH_PATH}",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=config["DARAJA_TIMEOUT"],
            )
            data = response.json() if response.content else {}
        except (requests.RequestException, ValueError) as error:
            current_app.logger.error(
                "Daraja STK push request failed: %s", type(error).__name__
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
    def query_stk_status(checkout_request_id):
        """Reconcile an STK Push with Daraja using the backend's own credentials.

        The callback endpoint is unauthenticated, so its ``ResultCode`` must
        never be trusted as proof of payment. This server-to-server query is
        authenticated with the consumer key/secret that only the backend holds,
        making it the authoritative source for whether the payment actually
        succeeded. ``checkout_request_id`` is supplied by Daraja, not the client.
        """
        config = MpesaService._config()
        access_token = MpesaService.get_access_token()
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

        try:
            response = requests.post(
                f"{config['DARAJA_BASE_URL']}{STK_QUERY_PATH}",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=config["DARAJA_TIMEOUT"],
            )
            data = response.json() if response.content else {}
        except (requests.RequestException, ValueError) as error:
            current_app.logger.error(
                "Daraja STK query failed: %s", type(error).__name__
            )
            raise ApiError(
                "Could not verify the M-Pesa payment.",
                502,
                ErrorCode.MPESA_REQUEST_FAILED,
            )

        if not isinstance(data, dict):
            data = {}

        return data

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

        try:
            response = MpesaService.send_stk_push(
                amount=amount,
                phone=phone,
                account_reference=account_reference,
            )
        except ApiError as error:
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
            "STK push initiated: mpesa_transaction=%s checkout_request_id=%s",
            mpesa_transaction.id,
            mpesa_transaction.checkout_request_id,
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

        mpesa_transaction = (
            MpesaTransaction.query.filter_by(
                checkout_request_id=checkout_request_id
            )
            .with_for_update()
            .first()
        )

        if not mpesa_transaction:
            current_app.logger.warning(
                "Received M-Pesa callback for unknown checkout_request_id=%s",
                checkout_request_id,
            )
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

        # PENDING and RECONCILIATION_PENDING are reprocessed below. Record the
        # callback envelope (attacker-controlled; never trusted for a credit).
        mpesa_transaction.result_code = truncate(parsed_callback["result_code"], 10)
        mpesa_transaction.result_desc = truncate(
            parsed_callback["result_desc"], RESULT_DESC_MAX_LENGTH
        )
        mpesa_transaction.transaction_date = truncate(
            parsed_callback["transaction_date"], 20
        )

        # The callback's ResultCode is attacker-controlled and must not be
        # trusted. Reconcile with Daraja (authenticated server-to-server) to
        # learn the actual outcome.
        try:
            query_result = MpesaService.query_stk_status(checkout_request_id)
        except ApiError:
            # Could not reach Daraja: do NOT credit and do NOT mark the deposit
            # failed. Keep it recoverable so a later callback or reconciliation
            # can still resolve it.
            mpesa_transaction.status = MpesaTransactionStatus.RECONCILIATION_PENDING
            MpesaService._record_reconciliation_attempt(mpesa_transaction)
            db.session.commit()
            current_app.logger.error(
                "M-Pesa reconciliation unreachable; keeping deposit recoverable: "
                "mpesa_transaction=%s",
                mpesa_transaction.id,
            )
            return mpesa_transaction

        query_code = str(query_result.get("ResultCode"))

        if query_code == "0":
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
                "M-Pesa payment definitively failed: mpesa_transaction=%s code=%s",
                mpesa_transaction.id,
                query_code,
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
            "M-Pesa reconciliation inconclusive; deposit stays recoverable: "
            "mpesa_transaction=%s code=%s",
            mpesa_transaction.id,
            query_code,
        )
        return mpesa_transaction

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

            if receipt_number:
                # Only ever set the receipt; never overwrite a stored one with
                # ``None`` (the recovery path has no receipt to report).
                mpesa_transaction.mpesa_receipt_number = receipt_number

            mpesa_transaction.status = MpesaTransactionStatus.COMPLETED
            mpesa_transaction.transaction = transaction

            db.session.commit()

        except IntegrityError:
            # The database-level backstop fired: this payment is already
            # credited. Never retry the credit.
            db.session.rollback()
            current_app.logger.warning(
                "Duplicate M-Pesa credit prevented for checkout_request_id=%s",
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

        current_app.logger.info(
            "M-Pesa deposit completed: mpesa_transaction=%s transaction=%s",
            mpesa_transaction.id,
            mpesa_transaction.transaction_id,
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
        if not checkout_request_id:
            # Nothing to reconcile against Daraja; leave it recoverable.
            current_app.logger.warning(
                "M-Pesa deposit cannot be reconciled without a "
                "checkout_request_id: mpesa_transaction=%s",
                mpesa_transaction_id,
            )
            return "reconciliation_pending"

        # No database lock and no open transaction is held for this call.
        try:
            query_result = MpesaService.query_stk_status(checkout_request_id)
        except ApiError:
            query_result = None

        mpesa_transaction = MpesaService._lock_mpesa_transaction(
            mpesa_transaction_id
        )

        if mpesa_transaction is None:
            db.session.rollback()
            current_app.logger.warning(
                "M-Pesa deposit disappeared during recovery: mpesa_transaction=%s",
                mpesa_transaction_id,
            )
            return "errors"

        # Re-check under the lock: a callback may have resolved this deposit
        # while the Daraja query was in flight.
        if MpesaTransactionStatus.is_terminal(mpesa_transaction.status):
            db.session.rollback()
            current_app.logger.info(
                "Skipping M-Pesa deposit resolved concurrently: "
                "mpesa_transaction=%s status=%s",
                mpesa_transaction.id,
                mpesa_transaction.status,
            )
            return "skipped"

        if query_result is None:
            # Daraja unreachable: keep it recoverable, never failed.
            mpesa_transaction.status = MpesaTransactionStatus.RECONCILIATION_PENDING
            MpesaService._record_reconciliation_attempt(mpesa_transaction)
            db.session.commit()
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
                return "credited"

            # The credit was refused or rolled back (for example the database
            # backstop fired); report it rather than counting a phantom credit.
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
                "M-Pesa payment definitively failed: mpesa_transaction=%s code=%s",
                mpesa_transaction.id,
                query_code,
            )
            return "failed"

        # Inconclusive / unrecognised non-zero result: keep recoverable.
        mpesa_transaction.status = MpesaTransactionStatus.RECONCILIATION_PENDING
        MpesaService._record_reconciliation_attempt(mpesa_transaction, query_result)
        db.session.commit()
        current_app.logger.info(
            "M-Pesa reconciliation inconclusive; deposit stays recoverable: "
            "mpesa_transaction=%s code=%s",
            mpesa_transaction.id,
            query_code,
        )
        return "reconciliation_pending"

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
