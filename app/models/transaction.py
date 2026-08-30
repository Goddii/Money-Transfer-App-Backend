from datetime import datetime
from app.extensions import db
from app.utils.helpers import money_to_string


class TransactionType:
    """Transaction types used by the MVP.

    The values match the type vocabulary already used by the existing admin
    audit log UI ("Transfer", "Deposit").
    """

    TRANSFER = 'Transfer'
    DEPOSIT = 'Deposit'
    SERVICE_PAYMENT = 'ServicePayment'


class TransactionStatus:
    """Status vocabulary already used by the schema and the admin UI."""

    PENDING = 'Pending'
    COMPLETED = 'Completed'
    FAILED = 'Failed'


class Transaction(db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    tx_code = db.Column(db.String(20), unique=True, nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    fee = db.Column(db.Numeric(12, 2), default=0.00)
    status = db.Column(db.String(20), default='Completed')
    tx_type = db.Column(db.String(50), nullable=False)
    note = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    sender = db.relationship('User', foreign_keys=[sender_id])
    receiver = db.relationship('User', foreign_keys=[receiver_id])

    @staticmethod
    def _party_summary(user):
        if not user:
            return None

        return {
            'id': user.id,
            'name': user.name,
        }

    def to_dict(self, current_user_id=None):
        """Return a safe representation of the transaction.

        Only MVP fields are exposed. Internal fields belonging to the
        counterparty (email, status, role, password hash) are never included.
        """
        data = {
            'id': self.id,
            'tx_code': self.tx_code,
            'amount': money_to_string(self.amount),
            'fee': money_to_string(self.fee),
            'status': self.status,
            'tx_type': self.tx_type,
            'note': self.note,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'sender': self._party_summary(self.sender),
            'receiver': self._party_summary(self.receiver),
        }

        if current_user_id is not None:
            if self.sender_id == current_user_id:
                data['direction'] = 'out'
            elif self.receiver_id == current_user_id:
                data['direction'] = 'in'

        return data
