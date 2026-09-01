from datetime import datetime

from app.extensions import db
from app.utils.helpers import mask_phone_number, money_to_string


class MpesaTransactionStatus:
    """Lifecycle of an M-Pesa deposit request.

    PENDING              - STK push accepted; awaiting callback/reconciliation.
    RECONCILIATION_PENDING - Payment may have succeeded but server-side
                            confirmation is currently inconclusive, unavailable,
                            or cannot yet be credited safely (for example the
                            callback reported a different amount). Always
                            recoverable.
    COMPLETED            - Daraja server-to-server confirmation succeeded and the
                            wallet was credited.
    FAILED               - Payment failure/cancellation definitively established.
    MANUAL_REVIEW_REQUIRED - Reconciliation exhausted its automatic budget
                            (e.g. hit MPESA_MAX_RECONCILIATION_ATTEMPTS) without a
                            definitive Daraja outcome. The deposit is held, never
                            auto-credited and never auto-failed; it is excluded
                            from automatic recovery and must be resolved by a
                            human. This is a terminal hold state, not a payment
                            failure.
    """

    PENDING = 'Pending'
    RECONCILIATION_PENDING = 'ReconciliationPending'
    COMPLETED = 'Completed'
    FAILED = 'Failed'
    MANUAL_REVIEW_REQUIRED = 'ManualReviewRequired'

    # States from which the deposit can no longer change (terminal).
    TERMINAL_STATUSES = (COMPLETED, FAILED, MANUAL_REVIEW_REQUIRED)

    # States that the recovery service will (re)process. Manual review is a
    # terminal hold and is deliberately NOT recoverable: once a deposit enters
    # it, no automatic Daraja reconciliation (sweeper/user/admin/callback) runs.
    RECOVERABLE_STATUSES = (PENDING, RECONCILIATION_PENDING)

    @classmethod
    def is_terminal(cls, status):
        return status in cls.TERMINAL_STATUSES

    @classmethod
    def is_recoverable(cls, status):
        return status in cls.RECOVERABLE_STATUSES


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
    # Widened to VARCHAR(50): the ``RECONCILIATION_PENDING`` status value is
    # 21 characters, which exceeds an earlier 20-character width and caused a
    # ``StringDataRightTruncation`` on PostgreSQL. 50 leaves ample headroom for
    # any future status value without risking silent truncation. Must stay in
    # sync with the Alembic migration that widens the column.
    status = db.Column(
        db.String(50), nullable=False, default=MpesaTransactionStatus.PENDING
    )
    merchant_request_id = db.Column(db.String(64), nullable=True)
    checkout_request_id = db.Column(db.String(64), unique=True, nullable=True)
    mpesa_receipt_number = db.Column(db.String(32), unique=True, nullable=True)
    result_code = db.Column(db.String(10), nullable=True)
    result_desc = db.Column(db.String(255), nullable=True)
    transaction_date = db.Column(db.String(20), nullable=True)
    # Reconciliation observability: the most recent Daraja server-to-server
    # query result and how often/when reconciliation has been attempted.
    query_result_code = db.Column(db.String(10), nullable=True)
    query_result_desc = db.Column(db.String(255), nullable=True)
    reconciliation_attempts = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
    last_reconciled_at = db.Column(db.DateTime, nullable=True)
    # The definitive failure reason when FAILED, or why a confirmed payment
    # could not yet be credited while RECONCILIATION_PENDING.
    failure_reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship('User')
    wallet = db.relationship('Wallet')
    transaction = db.relationship('Transaction')

    def to_dict(self):
        """Safe representation. Daraja credentials are never part of this row.

        The phone number is masked so the internal-facing representation never
        leaks the full subscriber identifier.
        """
        return {
            'id': self.id,
            'account_reference': self.account_reference,
            'phone_number': mask_phone_number(self.phone_number),
            'amount': money_to_string(self.amount),
            'status': self.status,
            'checkout_request_id': self.checkout_request_id,
            'merchant_request_id': self.merchant_request_id,
            'mpesa_receipt_number': self.mpesa_receipt_number,
            'transaction_id': self.transaction_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def to_status_dict(self):
        """Public, client-facing status payload for the user status endpoint.

        Deliberately omits ``checkout_request_id`` (not client-facing), merchant
        and receipt identifiers, and account reference. The phone number is
        masked. ``reconciliation`` fields are exposed only as coarse,
        non-sensitive status hints (attempts count and whether a definitive
        failure exists), never raw Daraja error text.
        """
        return {
            'id': self.id,
            'amount': money_to_string(self.amount),
            'status': self.status,
            'phone_number': mask_phone_number(self.phone_number),
            'mpesa_receipt_number': self.mpesa_receipt_number,
            'reconciliation_attempts': self.reconciliation_attempts or 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
