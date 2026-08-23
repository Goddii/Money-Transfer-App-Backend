"""Transaction business logic.

All money movement happens here inside a single database transaction so a
transfer can never partially succeed. Amounts are ``Decimal`` values only.
"""

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.services.wallet_service import WalletService
from app.utils.errors import ApiError, ErrorCode, log_exception
from app.utils.helpers import (
    ZERO_MONEY,
    generate_unique_tx_code,
    is_account_active,
    to_money,
)

# The Vyloc MVP does not define a peer-to-peer transfer fee schedule, so the
# existing ``transactions.fee`` default (0.00) is used and no fee is charged.
TRANSFER_FEE = ZERO_MONEY


class TransactionService:

    @staticmethod
    def transfer(sender, receiver_id, amount, note=None):
        """Move funds from the authenticated sender to another Vyloc user.

        Debit, credit, transaction record and ledger entries are committed
        together or rolled back together.
        """
        amount = to_money(amount)

        if receiver_id == sender.id:
            raise ApiError(
                "You cannot transfer funds to yourself.",
                400,
                ErrorCode.SELF_TRANSFER_NOT_ALLOWED,
            )

        receiver = db.session.get(User, receiver_id)

        if not receiver:
            raise ApiError(
                "Receiver not found.", 404, ErrorCode.RECEIVER_NOT_FOUND
            )

        if not is_account_active(receiver):
            raise ApiError(
                "Receiver cannot receive funds at this time.",
                400,
                ErrorCode.RECEIVER_NOT_ELIGIBLE,
            )

        try:
            # Lock both wallets (in wallet id order) for the whole operation.
            wallets = WalletService.get_locked_wallets([sender.id, receiver.id])

            sender_wallet = wallets.get(sender.id)
            receiver_wallet = wallets.get(receiver.id)

            if not sender_wallet:
                raise ApiError(
                    "Wallet not found for this account.",
                    404,
                    ErrorCode.WALLET_NOT_FOUND,
                )

            if not receiver_wallet:
                raise ApiError(
                    "Receiver cannot receive funds at this time.",
                    400,
                    ErrorCode.RECEIVER_NOT_ELIGIBLE,
                )

            transaction = Transaction(
                tx_code=generate_unique_tx_code(Transaction),
                sender_id=sender.id,
                receiver_id=receiver.id,
                amount=amount,
                fee=TRANSFER_FEE,
                status=TransactionStatus.COMPLETED,
                tx_type=TransactionType.TRANSFER,
                note=note,
            )
            db.session.add(transaction)

            WalletService.debit(
                sender_wallet,
                amount,
                reference=transaction.tx_code,
                description=f"Transfer to {receiver.name}",
                transaction=transaction,
            )

            WalletService.credit(
                receiver_wallet,
                amount,
                reference=transaction.tx_code,
                description=f"Transfer from {sender.name}",
                transaction=transaction,
            )

            db.session.commit()

        except ApiError:
            db.session.rollback()
            raise

        except SQLAlchemyError:
            db.session.rollback()
            log_exception("transfer")
            raise ApiError(
                "Transfer could not be completed.",
                500,
                ErrorCode.TRANSFER_FAILED,
            )

        except Exception:
            db.session.rollback()
            raise

        return transaction

    @staticmethod
    def record_deposit(user, wallet, amount, reference, description=None):
        """Create the internal transaction and ledger entry for a deposit.

        Staged on the current session; the caller commits so the deposit can be
        committed atomically with the M-Pesa transaction state.
        """
        amount = to_money(amount)

        transaction = Transaction(
            tx_code=generate_unique_tx_code(Transaction),
            sender_id=None,
            receiver_id=user.id,
            amount=amount,
            fee=ZERO_MONEY,
            status=TransactionStatus.COMPLETED,
            tx_type=TransactionType.DEPOSIT,
            note=description,
        )
        db.session.add(transaction)

        WalletService.credit(
            wallet,
            amount,
            reference=reference,
            description=description,
            transaction=transaction,
        )

        return transaction

    @staticmethod
    def list_for_user(user_id, page=1, per_page=20):
        """Return only transactions where the user is sender or receiver."""
        statement = (
            select(Transaction)
            .options(joinedload(Transaction.sender), joinedload(Transaction.receiver))
            .where(
                or_(
                    Transaction.sender_id == user_id,
                    Transaction.receiver_id == user_id,
                )
            )
            .order_by(Transaction.timestamp.desc(), Transaction.id.desc())
        )

        return db.paginate(
            statement,
            page=page,
            per_page=per_page,
            error_out=False,
            max_per_page=100,
        )

    @staticmethod
    def get_for_user(transaction_id, user_id):
        """Load a transaction the user participated in.

        Any other transaction is reported as not found so unrelated records are
        never exposed.
        """
        transaction = Transaction.query.filter(
            Transaction.id == transaction_id,
            or_(
                Transaction.sender_id == user_id,
                Transaction.receiver_id == user_id,
            ),
        ).first()

        if not transaction:
            raise ApiError(
                "Transaction not found.", 404, ErrorCode.TRANSACTION_NOT_FOUND
            )

        return transaction
