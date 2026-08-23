from datetime import datetime

from app.extensions import db
from app.utils.helpers import money_to_string


class MpesaTransactionStatus:
    """Lifecycle of an M-Pesa deposit request."""

    PENDING = 'Pending'
    COMPLETED = 'Completed'
    FAILED = 'Failed'


class MpesaTransaction(db.Model):
    """An M-Pesa (Daraja STK Push) deposit request and its result.

    A row is created when the STK Push is initiated and is only marked
    ``Completed`` once Safaricom confirms the payment through the callback.
    ``checkout_request_id`` is unique so a repeated callback resolves to the
    same row, which makes callback processing idempotent.
    """

    __tablename__ = 'mpesa_transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False, index=True
    )
    wallet_id = db.Column(
        db.Integer, db.ForeignKey('wallets.id'), nullable=False, index=True
    )
    transaction_id = db.Column(
        db.Integer, db.ForeignKey('transactions.id'), nullable=True, index=True
    )
    account_reference = db.Column(db.String(20), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default=MpesaTransactionStatus.PENDING
    )
    merchant_request_id = db.Column(db.String(64), nullable=True)
    checkout_request_id = db.Column(db.String(64), unique=True, nullable=True)
    mpesa_receipt_number = db.Column(db.String(32), unique=True, nullable=True)
    result_code = db.Column(db.String(10), nullable=True)
    result_desc = db.Column(db.String(255), nullable=True)
    transaction_date = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship('User')
    wallet = db.relationship('Wallet')
    transaction = db.relationship('Transaction')

    def to_dict(self):
        """Safe representation. Daraja credentials are never part of this row."""
        return {
            'id': self.id,
            'account_reference': self.account_reference,
            'phone_number': self.phone_number,
            'amount': money_to_string(self.amount),
            'status': self.status,
            'checkout_request_id': self.checkout_request_id,
            'merchant_request_id': self.merchant_request_id,
            'mpesa_receipt_number': self.mpesa_receipt_number,
            'transaction_id': self.transaction_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
