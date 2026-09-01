from datetime import datetime

from app.extensions import db
from app.utils.helpers import money_to_string


class LedgerEntryType:
    """Direction of a wallet balance change."""

    CREDIT = 'CREDIT'
    DEBIT = 'DEBIT'


class WalletLedger(db.Model):
    """Immutable audit trail of every wallet balance change.

    ``wallets.balance`` holds the current state while this table records how
    that state was reached (see README "Database").

    The unique constraint on ``(wallet_id, entry_type, reference)`` is the
    database-level guard that prevents the same external event (for example a
    repeated M-Pesa callback) from crediting a wallet twice.
    """

    __tablename__ = 'wallet_ledger'

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(
        db.Integer, db.ForeignKey('wallets.id'), nullable=False, index=True
    )
    transaction_id = db.Column(
        db.Integer, db.ForeignKey('transactions.id'), nullable=True, index=True
    )
    entry_type = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    balance_before = db.Column(db.Numeric(12, 2), nullable=False)
    balance_after = db.Column(db.Numeric(12, 2), nullable=False)
    reference = db.Column(db.String(64), nullable=True, index=True)
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    wallet = db.relationship('Wallet', back_populates='ledger_entries')
    transaction = db.relationship('Transaction')

    __table_args__ = (
        db.UniqueConstraint(
            'wallet_id',
            'entry_type',
            'reference',
            name='unique_wallet_ledger_reference',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'wallet_id': self.wallet_id,
            'transaction_id': self.transaction_id,
            'entry_type': self.entry_type,
            'amount': money_to_string(self.amount),
            'balance_before': money_to_string(self.balance_before),
            'balance_after': money_to_string(self.balance_after),
            'reference': self.reference,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
