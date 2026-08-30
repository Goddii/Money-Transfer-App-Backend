"""Simulated service payment models.

Providers are configuration-driven (not database-driven) because all three
are static simulations with no external API. The ``ServiceProvider`` model
exists only to make the available services discoverable via the API without
hardcoding them in a route handler.

``ServicePayment`` records every attempt to pay a service provider. It
mirrors the ``mpesa_transactions`` pattern: a row is created when the
payment is initiated, then updated once the simulated provider returns a
definitive outcome. Service-specific metadata (electricity token, water
receipt, airtime confirmation) is stored as a JSON column so the schema
does not need to grow for each new service type.
"""

from datetime import datetime

from app.extensions import db
from app.utils.helpers import money_to_string


class ServiceType:
    """Supported simulated service types."""

    ELECTRICITY = "ELECTRICITY"
    WATER = "WATER"
    AIRTIME = "AIRTIME"

    ALL = (ELECTRICITY, WATER, AIRTIME)


class ServicePaymentStatus:
    """Lifecycle of a service payment.

    INITIATED  - Record created, wallet about to be debited.
    PROCESSING - Wallet debited, simulated provider running.
    COMPLETED  - Provider returned success; payment is final.
    PENDING    - Provider returned pending; awaiting reconciliation.
    FAILED     - Provider returned failure; wallet refunded if debited.
    REFUNDED   - Wallet was debited but the payment ultimately failed
                 and a refund ledger entry has been written.
    """

    INITIATED = "Initiated"
    PROCESSING = "Processing"
    COMPLETED = "Completed"
    PENDING = "Pending"
    FAILED = "Failed"
    REFUNDED = "Refunded"

    # States from which the payment can no longer change (terminal).
    TERMINAL_STATUSES = (COMPLETED, FAILED, REFUNDED)

    # States that can transition to a final outcome via reconciliation.
    RECOVERABLE_STATUSES = (INITIATED, PROCESSING, PENDING)

    @classmethod
    def is_terminal(cls, status):
        return status in cls.TERMINAL_STATUSES

    @classmethod
    def is_recoverable(cls, status):
        return status in cls.RECOVERABLE_STATUSES


class ServiceProvider(db.Model):
    """Static registry of available simulated services.

    Rows are seeded once and never modified at runtime. This keeps the
    provider list discoverable through the API without hardcoding in routes.
    """

    __tablename__ = "service_providers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    service_type = db.Column(db.String(20), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        # ``service_type`` is the canonical key: it is the exact enum value the
        # POST /api/service-payments contract expects in its ``service_type``
        # field, and it matches ``ServicePayment.to_dict()``. ``type`` is kept
        # as a legacy alias so older clients keep working.
        return {
            "id": self.id,
            "name": self.name,
            "service_type": self.service_type,
            "type": self.service_type,
            "display_name": self.display_name,
            "description": self.description,
            "is_active": self.is_active,
        }


class ServicePayment(db.Model):
    """A simulated service payment attempt.

    Pattern mirrors ``MpesaTransaction``: created on initiation, updated
    when the provider resolves. Linked to a ``Transaction`` row so the
    payment appears in the user's transaction history.
    """

    __tablename__ = "service_payments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    wallet_id = db.Column(
        db.Integer, db.ForeignKey("wallets.id"), nullable=False, index=True
    )
    transaction_id = db.Column(
        db.Integer, db.ForeignKey("transactions.id"), nullable=True, index=True
    )
    service_type = db.Column(db.String(20), nullable=False)
    account_number = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default=ServicePaymentStatus.INITIATED
    )
    payment_reference = db.Column(db.String(30), unique=True, nullable=False)
    # Optional client-supplied idempotency key. When present, a repeated
    # request with the same key returns the already-created payment instead of
    # debiting the wallet a second time. NULL for requests that do not supply
    # one, so the unique constraint (which ignores NULLs) does not reject them.
    idempotency_key = db.Column(db.String(64), unique=True, nullable=True, index=True)
    provider_reference = db.Column(db.String(64), nullable=True)
    failure_reason = db.Column(db.String(255), nullable=True)
    # Service-specific metadata (token, units, receipt, confirmation, etc.)
    result_metadata = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship("User")
    wallet = db.relationship("Wallet")
    transaction = db.relationship("Transaction")

    def to_dict(self, current_user_id=None):
        """Safe representation for API responses.

        The account number is masked to avoid leaking sensitive identifiers.
        """
        from app.utils.helpers import mask_phone_number

        data = {
            "id": self.id,
            "service_type": self.service_type,
            "account_number": mask_phone_number(self.account_number),
            "amount": money_to_string(self.amount),
            "status": self.status,
            "payment_reference": self.payment_reference,
            "transaction_id": self.transaction_id,
            "result_metadata": self.result_metadata,
            "failure_reason": self.failure_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        return data
