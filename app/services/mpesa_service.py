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
    def process_callback(parsed_callback):
        """Process a Daraja STK callback exactly once.

        The unauthenticated callback is treated only as a notification. Before
        any wallet is credited, the payment is reconciled with Daraja using the
        backend's own credentials so a forged callback cannot manufacture money.
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

        if mpesa_transaction.status != MpesaTransactionStatus.PENDING:
            current_app.logger.info(
                "Ignoring duplicate M-Pesa callback: mpesa_transaction=%s status=%s",
                mpesa_transaction.id,
                mpesa_transaction.status,
            )
            db.session.rollback()
            return mpesa_transaction

        mpesa_transaction.result_code = truncate(parsed_callback["result_code"], 10)
        mpesa_transaction.result_desc = truncate(
            parsed_callback["result_desc"], RESULT_DESC_MAX_LENGTH
        )
        mpesa_transaction.transaction_date = truncate(
            parsed_callback["transaction_date"], 20
        )

        # R1: the callback's ResultCode is attacker-controlled and must not be
        # trusted. Reconcile with Daraja (authenticated server-to-server) to
        # learn the actual outcome.
        try:
            query_result = MpesaService.query_stk_status(checkout_request_id)
        except ApiError:
            # Could not reach Daraja: do NOT credit and do NOT mark the deposit
            # failed, so a later callback retry can still reconcile it.
            db.session.commit()
            current_app.logger.error(
                "M-Pesa reconciliation failed; keeping deposit pending: "
                "mpesa_transaction=%s",
                mpesa_transaction.id,
            )
            return mpesa_transaction

        if str(query_result.get("ResultCode")) != "0":
            mpesa_transaction.status = MpesaTransactionStatus.FAILED
            if not mpesa_transaction.result_desc:
                mpesa_transaction.result_desc = truncate(
                    query_result.get("ResultDesc"), RESULT_DESC_MAX_LENGTH
                )
            db.session.commit()
            current_app.logger.info(
                "M-Pesa payment not confirmed by Daraja: "
                "mpesa_transaction=%s query_code=%s",
                mpesa_transaction.id,
                query_result.get("ResultCode"),
            )
            return mpesa_transaction

        return MpesaService._credit_confirmed_deposit(
            mpesa_transaction,
            callback_amount=parsed_callback["amount"],
            receipt_number=parsed_callback["mpesa_receipt_number"],
        )

    @staticmethod
    def _credit_confirmed_deposit(mpesa_transaction, callback_amount, receipt_number):
        """Credit the wallet for a deposit Daraja has confirmed as paid.

        The credited amount is always the stored requested amount, never the
        callback's reported amount, so a forged amount cannot inflate a balance.
        Amount mismatch (defence-in-depth) still rejects the deposit.
        """
        expected_amount = to_money(mpesa_transaction.amount)
        checkout_request_id = mpesa_transaction.checkout_request_id

        if callback_amount is not None:
            try:
                callback_amount = to_money(callback_amount)
            except (ArithmeticError, ValueError):
                callback_amount = None

            if callback_amount is not None and callback_amount != expected_amount:
                mpesa_transaction.status = MpesaTransactionStatus.FAILED
                mpesa_transaction.result_desc = truncate(
                    "Confirmed amount did not match the requested deposit amount.",
                    RESULT_DESC_MAX_LENGTH,
                )
                db.session.commit()
                current_app.logger.error(
                    "M-Pesa callback amount mismatch: mpesa_transaction=%s",
                    mpesa_transaction.id,
                )
                return mpesa_transaction

        receipt_number = truncate(receipt_number, 32)
        # The receipt is the natural idempotency key; fall back to the checkout
        # id when Safaricom omits it.
        reference = receipt_number or checkout_request_id

        try:
            wallet = WalletService.get_locked_wallet(mpesa_transaction.user_id)

            transaction = TransactionService.record_deposit(
                user=mpesa_transaction.user,
                wallet=wallet,
                amount=expected_amount,
                reference=reference,
                description="M-Pesa deposit",
            )

            mpesa_transaction.mpesa_receipt_number = receipt_number
            mpesa_transaction.status = MpesaTransactionStatus.COMPLETED
            mpesa_transaction.transaction = transaction

            db.session.commit()

        except IntegrityError:
            # A concurrent duplicate callback already credited this payment.
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
    def reconcile_pending():
        """Recover PENDING deposits by reconciling each with Daraja.

        Provides the R2 recovery path for deposits whose callback never arrived
        (or arrived before persistence): any PENDING row with a
        ``checkout_request_id`` is re-checked against Daraja and, if paid,
        credited. Returns a summary count dict.
        """
        summary = {"credited": 0, "failed": 0, "pending": 0}

        pending = MpesaTransaction.query.filter_by(
            status=MpesaTransactionStatus.PENDING
        ).all()

        for mpesa_transaction in pending:
            if not mpesa_transaction.checkout_request_id:
                # Nothing to reconcile against Daraja.
                summary["pending"] += 1
                continue

            try:
                query_result = MpesaService.query_stk_status(
                    mpesa_transaction.checkout_request_id
                )
            except ApiError:
                summary["pending"] += 1
                continue

            if str(query_result.get("ResultCode")) != "0":
                mpesa_transaction.status = MpesaTransactionStatus.FAILED
                if not mpesa_transaction.result_desc:
                    mpesa_transaction.result_desc = truncate(
                        query_result.get("ResultDesc"), RESULT_DESC_MAX_LENGTH
                    )
                db.session.commit()
                summary["failed"] += 1
                continue

            MpesaService._credit_confirmed_deposit(
                mpesa_transaction, callback_amount=None, receipt_number=None
            )
            summary["credited"] += 1

        return summary
