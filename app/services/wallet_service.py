"""Wallet business logic.

Every balance change goes through this service so that a matching
``wallet_ledger`` entry is always written. Balances are handled exclusively as
``Decimal`` values.
"""

from app.extensions import db
from app.models.wallet import Wallet
from app.models.wallet_ledger import LedgerEntryType, WalletLedger
from app.utils.errors import ApiError, ErrorCode
from app.utils.helpers import ZERO_MONEY, to_money


class WalletService:

    @staticmethod
    def create_wallet(user_id):
        """Build a wallet for a user and stage it on the current session.

        The caller controls the transaction boundary so wallet creation can be
        committed together with user creation.
        """
        wallet = Wallet(user_id=user_id, balance=ZERO_MONEY)
        db.session.add(wallet)

        return wallet

    @staticmethod
    def get_wallet_by_user_id(user_id):
        return Wallet.query.filter_by(user_id=user_id).first()

    @staticmethod
    def get_wallet_or_error(user_id, message="Wallet not found."):
        wallet = WalletService.get_wallet_by_user_id(user_id)

        if not wallet:
            raise ApiError(message, 404, ErrorCode.WALLET_NOT_FOUND)

        return wallet

    @staticmethod
    def get_locked_wallet(user_id, message="Wallet not found."):
        """Load a wallet with a row lock for a balance-changing operation."""
        wallet = (
            Wallet.query.filter_by(user_id=user_id).with_for_update().first()
        )

        if not wallet:
            raise ApiError(message, 404, ErrorCode.WALLET_NOT_FOUND)

        return wallet

    @staticmethod
    def get_locked_wallets(user_ids):
        """Load several wallets with row locks in a deterministic order.

        Locking in ``wallets.id`` order prevents deadlocks when two users
        transfer to each other at the same time.
        """
        wallets = (
            Wallet.query.filter(Wallet.user_id.in_(list(user_ids)))
            .order_by(Wallet.id)
            .with_for_update()
            .all()
        )

        return {wallet.user_id: wallet for wallet in wallets}

    @staticmethod
    def _record_entry(
        wallet,
        entry_type,
        amount,
        balance_before,
        balance_after,
        reference=None,
        description=None,
        transaction=None,
    ):
        entry = WalletLedger(
            wallet=wallet,
            entry_type=entry_type,
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            reference=reference,
            description=description,
            transaction=transaction,
        )
        db.session.add(entry)

        return entry

    @staticmethod
    def credit(wallet, amount, reference=None, description=None, transaction=None):
        """Credit a wallet and record the ledger entry. Does not commit."""
        amount = to_money(amount)
        balance_before = to_money(wallet.balance or ZERO_MONEY)
        balance_after = balance_before + amount

        wallet.balance = balance_after

        return WalletService._record_entry(
            wallet,
            LedgerEntryType.CREDIT,
            amount,
            balance_before,
            balance_after,
            reference=reference,
            description=description,
            transaction=transaction,
        )

    @staticmethod
    def debit(wallet, amount, reference=None, description=None, transaction=None):
        """Debit a wallet and record the ledger entry. Does not commit."""
        amount = to_money(amount)
        balance_before = to_money(wallet.balance or ZERO_MONEY)

        if balance_before < amount:
            raise ApiError(
                "Insufficient wallet balance.",
                400,
                ErrorCode.INSUFFICIENT_BALANCE,
            )

        balance_after = balance_before - amount

        wallet.balance = balance_after

        return WalletService._record_entry(
            wallet,
            LedgerEntryType.DEBIT,
            amount,
            balance_before,
            balance_after,
            reference=reference,
            description=description,
            transaction=transaction,
        )
