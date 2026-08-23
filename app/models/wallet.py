from datetime import datetime
from app.extensions import db
from app.utils.helpers import money_to_string


class Wallet(db.Model):
    __tablename__ = 'wallets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    balance = db.Column(db.Numeric(12, 2), default=0.00)
    currency = db.Column(db.String(3), default='USD')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    ledger_entries = db.relationship(
        'WalletLedger',
        back_populates='wallet',
        cascade='all, delete-orphan',
        order_by='WalletLedger.id.desc()',
    )

    # A user owns exactly one wallet (see README "Core Relationships").
    __table_args__ = (
        db.UniqueConstraint('user_id', name='unique_wallet_user'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'balance': money_to_string(self.balance),
            'currency': self.currency,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
