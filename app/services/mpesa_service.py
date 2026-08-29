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
        try:
            query_result = MpesaService.query_stk_status(checkout_request_id)
        except ApiError:
            # Could not reach Daraja: do NOT credit and do NOT mark the deposit
            # failed. Acquire the row lock, re-read the latest state, and only
            # then keep the deposit recoverable — a concurrent callback/sweeper
            # may already have credited it.
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
    def _callback_keep_recoverable(mpesa_transaction_id, parsed_callback, checkout_request_id):
        """Daraja unreachable from the callback: keep the deposit recoverable.

        Re-acquires the row lock, re-reads the latest state, and only then marks
        the deposit ``RECONCILIATION_PENDING``. A concurrent callback/sweeper may
        already have credited or failed the deposit, in which case we leave it
        untouched rather than clobbering its terminal state.
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
        MpesaService._record_reconciliation_attempt(locked)
        db.session.commit()
        current_app.logger.error(
            "M-Pesa reconciliation unreachable; keeping deposit recoverable: "
            "mpesa_transaction=%s",
            locked.id,
        )
        return locked

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
                "MPESA_EVENT=RECONCILIATION_ERROR mpesa_transaction=%s "
                "reason=disappeared",
                mpesa_transaction_id,
            )
            return "errors"

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
            # Daraja unreachable: keep it recoverable, never failed.
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

        try:
            conn = db.engine.connect()
            acquired = conn.exec_driver_sql(
                "SELECT pg_try_advisory_lock(:key)",
                {"key": MpesaService._sweeper_leader_lock_id(app)},
            ).scalar()
        except Exception:
            app.logger.exception("MPESA_EVENT=SWEEPER_LEADERSHIP_ERROR")
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
            conn.exec_driver_sql(
                "SELECT pg_advisory_unlock(:key)",
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
