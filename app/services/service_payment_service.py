"""Service payment business logic.

Orchestrates the full lifecycle of a simulated service payment:
validation → wallet debit → simulated provider → status update → refund on failure.

Integrates with the existing ``WalletService`` and ``TransactionService``
to maintain financial integrity. Never manipulates wallet balances directly.
"""

import secrets
from decimal import Decimal

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.service_payment import (
    ServicePayment,
    ServicePaymentStatus,
    ServiceProvider,
    ServiceType,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.wallet import Wallet
from app.services.providers import resolve_provider
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService
from app.utils.errors import ApiError, ErrorCode, log_exception
from app.utils.helpers import ZERO_MONEY, generate_unique_tx_code, to_money


class ServicePaymentService:

    # --- listing available services ---

    @staticmethod
    def list_services():
        """Return all active service providers."""
        providers = ServiceProvider.query.filter_by(is_active=True).all()
        return [p.to_dict() for p in providers]

    # --- initiating a payment ---

    @staticmethod
    def initiate_payment(user, service_type, account_number, amount, idempotency_key=None):
        """Process a service payment from the user's wallet.

        Flow:
        1. Validate service type and account number
        2. Validate and normalize the amount
        3. Create a ServicePayment record (INITIATED)
        4. Create a Transaction record
        5. Debit the wallet using WalletService
        6. Run the simulated provider
        7. Update ServicePayment and Transaction status
        8. On failure: refund the wallet

        All operations within a single database transaction for atomicity.
        """
        # Validate service type.
        if service_type not in ServiceType.ALL:
            raise ApiError(
                f"Unknown service type: {service_type}",
                400,
                ErrorCode.INVALID_SERVICE_TYPE,
            )

        # Idempotency: a repeat of a request carrying the same key returns the
        # already-created payment without debiting the wallet a second time.
        if idempotency_key:
            existing = ServicePayment.query.filter_by(
                user_id=user.id, idempotency_key=idempotency_key
            ).first()
            if existing:
                return existing

        # Resolve and validate the provider.
        provider_cls = resolve_provider(service_type)
        amount = to_money(amount)
        cleaned_account = provider_cls.validate(account_number, amount)

        # Get the wallet with row lock.
        wallet = WalletService.get_locked_wallet(
            user.id, message="Wallet not found for this account."
        )

        # Generate payment reference (unique).
        payment_reference = _generate_payment_reference()

        # Create the service payment record.
        service_payment = ServicePayment(
            user_id=user.id,
            wallet_id=wallet.id,
            service_type=service_type,
            account_number=cleaned_account,
            amount=amount,
            status=ServicePaymentStatus.INITIATED,
            payment_reference=payment_reference,
            idempotency_key=idempotency_key,
        )
        db.session.add(service_payment)
        db.session.flush()  # Get the service_payment.id

        # Create a Transaction record for history.
        transaction = Transaction(
            tx_code=generate_unique_tx_code(Transaction),
            sender_id=user.id,
            receiver_id=user.id,  # Self-referencing: money leaves the system.
            amount=amount,
            fee=ZERO_MONEY,
            status=TransactionStatus.PENDING,
            tx_type=TransactionType.SERVICE_PAYMENT,
            note=f"{service_type.title()} Payment",
        )
        db.session.add(transaction)
        db.session.flush()

        service_payment.transaction_id = transaction.id
        service_payment.status = ServicePaymentStatus.PROCESSING
        transaction.status = TransactionStatus.PENDING

        # Debit the wallet.
        WalletService.debit(
            wallet,
            amount,
            reference=payment_reference,
            description=f"{service_type.title()} Payment",
            transaction=transaction,
        )

        # Process the simulated provider.
        try:
            result = provider_cls.process(cleaned_account, amount, payment_reference)
        except Exception:
            db.session.rollback()
            log_exception("service_payment_provider")
            raise ApiError(
                "Service payment could not be processed.",
                500,
                ErrorCode.SERVICE_PAYMENT_FAILED,
            )

        # Update status based on provider result.
        if result.status == "COMPLETED":
            service_payment.status = ServicePaymentStatus.COMPLETED
            service_payment.result_metadata = result.metadata
            service_payment.provider_reference = result.metadata.get(
                "payment_reference"
            )
            transaction.status = TransactionStatus.COMPLETED
            transaction.note = f"{service_type.title()} Payment - Completed"

        elif result.status == "PENDING":
            service_payment.status = ServicePaymentStatus.PENDING
            service_payment.result_metadata = result.metadata
            transaction.status = TransactionStatus.PENDING
            transaction.note = f"{service_type.title()} Payment - Pending"

        elif result.status == "FAILED":
            service_payment.status = ServicePaymentStatus.FAILED
            service_payment.failure_reason = result.failure_reason

            # Refund the wallet.
            WalletService.credit(
                wallet,
                amount,
                reference=payment_reference,
                description=f"Refund: {service_type.title()} Payment (Failed)",
                transaction=transaction,
            )
            service_payment.status = ServicePaymentStatus.REFUNDED
            transaction.status = TransactionStatus.FAILED
            transaction.note = f"{service_type.title()} Payment - Failed/Refunded"

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # A duplicate idempotency key raced to create the payment first.
            # Return the surviving row so the caller sees exactly one debit.
            if idempotency_key:
                existing = ServicePayment.query.filter_by(
                    user_id=user.id, idempotency_key=idempotency_key
                ).first()
                if existing:
                    return existing
            raise ApiError(
                "Service payment already exists (duplicate reference).",
                409,
                ErrorCode.DUPLICATE_RESOURCE,
            )
        except SQLAlchemyError:
            db.session.rollback()
            log_exception("service_payment_commit")
            raise ApiError(
                "Service payment could not be completed.",
                500,
                ErrorCode.SERVICE_PAYMENT_FAILED,
            )

        return service_payment

    # --- reconciliation (for PENDING payments) ---

    @staticmethod
    def reconcile_payment(user_id, payment_id):
        """Reconcile a pending service payment.

        Re-runs the simulated provider to determine the outcome. For
        deterministic demo scenarios: anything other than the FAILED prefix
        (3333...) becomes COMPLETED, and the FAILED prefix becomes FAILED and is
        refunded. A PENDING result from the provider is treated as the provider
        having now settled, so the payment is finalized as COMPLETED rather than
        left in limbo with funds stranded.

        Idempotent: calling reconcile on a terminal payment is a no-op, and the
        payment row is re-locked before any wallet movement so concurrent calls
        cannot double-refund.
        """
        service_payment = ServicePayment.query.filter_by(
            id=payment_id, user_id=user_id
        ).first()

        if not service_payment:
            raise ApiError(
                "Service payment not found.",
                404,
                ErrorCode.SERVICE_PAYMENT_NOT_FOUND,
            )

        if ServicePaymentStatus.is_terminal(service_payment.status):
            return service_payment

        if not ServicePaymentStatus.is_recoverable(service_payment.status):
            raise ApiError(
                "This payment cannot be reconciled.",
                400,
                ErrorCode.SERVICE_PAYMENT_NOT_RECONCILABLE,
            )

        # Re-run the simulated provider.
        provider_cls = resolve_provider(service_payment.service_type)
        result = provider_cls.process(
            service_payment.account_number,
            service_payment.amount,
            service_payment.payment_reference,
        )

        wallet = WalletService.get_locked_wallet(user_id)

        # Re-lock the payment row and re-check its state. A concurrent reconcile
        # may already have finalized it; without this re-check a second caller
        # (past the earlier terminal check) could refund twice.
        service_payment = (
            ServicePayment.query.with_for_update()
            .filter_by(id=payment_id, user_id=user_id)
            .first()
        )
        if ServicePaymentStatus.is_terminal(service_payment.status):
            try:
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                log_exception("service_payment_reconcile")
                raise ApiError(
                    "Reconciliation could not be completed.",
                    500,
                    ErrorCode.SERVICE_PAYMENT_FAILED,
                )
            return service_payment

        transaction = None
        if service_payment.transaction_id:
            transaction = db.session.get(Transaction, service_payment.transaction_id)

        if result.status == "COMPLETED" or result.status == "PENDING":
            # A PENDING provider result is finalized as success on reconcile so
            # funds are not stranded in a perpetual pending state.
            service_payment.status = ServicePaymentStatus.COMPLETED
            service_payment.result_metadata = result.metadata
            service_payment.provider_reference = result.metadata.get(
                "payment_reference"
            )
            if transaction:
                transaction.status = TransactionStatus.COMPLETED
                transaction.note = (
                    f"{service_payment.service_type.title()} Payment - Completed"
                )

        elif result.status == "FAILED":
            service_payment.status = ServicePaymentStatus.FAILED
            service_payment.failure_reason = result.failure_reason

            # Refund the wallet, linked to the original transaction so the
            # ledger/transaction audit trail stays consistent.
            WalletService.credit(
                wallet,
                service_payment.amount,
                reference=service_payment.payment_reference,
                description=f"Refund: {service_payment.service_type.title()} Payment (Failed)",
                transaction=transaction,
            )

            if transaction:
                transaction.status = TransactionStatus.FAILED
                transaction.note = (
                    f"{service_payment.service_type.title()} Payment - Failed/Refunded"
                )

            service_payment.status = ServicePaymentStatus.REFUNDED

        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            log_exception("service_payment_reconcile")
            raise ApiError(
                "Reconciliation could not be completed.",
                500,
                ErrorCode.SERVICE_PAYMENT_FAILED,
            )

        return service_payment

    # --- listing and retrieval ---

    @staticmethod
    def list_for_user(user_id, page=1, per_page=20):
        """Return paginated service payments for a user."""
        from sqlalchemy import select

        statement = (
            select(ServicePayment)
            .filter(ServicePayment.user_id == user_id)
            .order_by(ServicePayment.created_at.desc(), ServicePayment.id.desc())
        )

        return db.paginate(
            statement,
            page=page,
            per_page=per_page,
            error_out=False,
            max_per_page=100,
        )

    @staticmethod
    def get_for_user(payment_id, user_id):
        """Return a single service payment owned by the user."""
        service_payment = ServicePayment.query.filter_by(
            id=payment_id, user_id=user_id
        ).first()

        if not service_payment:
            raise ApiError(
                "Service payment not found.",
                404,
                ErrorCode.SERVICE_PAYMENT_NOT_FOUND,
            )

        return service_payment


def _generate_payment_reference():
    """Generate a unique payment reference like VYL-SVC-A1B2C3."""
    random_part = secrets.token_hex(3).upper()
    return f"VYL-SVC-{random_part}"
