"""Shared helpers for money handling and transaction references."""

import secrets
from decimal import Decimal, ROUND_HALF_UP

# ``Numeric(12, 2)`` is used for every monetary column in the schema.
MONEY_EXPONENT = Decimal("0.01")
MONEY_MAX = Decimal("9999999999.99")
ZERO_MONEY = Decimal("0.00")

TX_CODE_PREFIX = "VYL"
TX_CODE_RANDOM_LENGTH = 10

# Single source of truth for the "active" account state. ``User.status`` and
# ``User.is_active`` were historically nullable, so both must be explicitly
# satisfied; legacy NULL values are therefore treated as inactive.
ACTIVE_STATUS = "Active"


def is_account_active(user):
    """Return ``True`` only when the account is active and not frozen.

    Both ``is_active`` and ``status == 'Active'`` must hold. Falsy/``None``
    values (including legacy NULL columns) are treated as inactive so they can
    never be granted a token or receive funds.
    """
    return bool(getattr(user, "is_active", False)) and user.status == ACTIVE_STATUS


def to_money(value):
    """Normalise a value to a 2-decimal-place :class:`~decimal.Decimal`.

    Monetary values are always handled as ``Decimal`` and never as ``float``.
    """
    if isinstance(value, Decimal):
        amount = value
    else:
        # ``str()`` keeps the exact literal so no binary float error is introduced.
        amount = Decimal(str(value))

    return amount.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def money_to_string(value):
    """Serialise a monetary value as a fixed 2-decimal-place string."""
    if value is None:
        return str(ZERO_MONEY)

    return str(to_money(value))


def generate_tx_code():
    """Generate a random transaction reference.

    ``transactions.tx_code`` is ``String(20)`` and unique, so the generated
    reference is deliberately short. Uniqueness is additionally guaranteed by
    the database constraint; callers should retry on conflict.
    """
    random_part = secrets.token_hex(TX_CODE_RANDOM_LENGTH // 2).upper()

    return f"{TX_CODE_PREFIX}{random_part}"


def generate_unique_tx_code(model, attempts=5):
    """Generate a ``tx_code`` that is not already present in ``model``.

    The database unique constraint remains the authoritative guard; this only
    avoids the common case of a collision.
    """
    for _ in range(attempts):
        tx_code = generate_tx_code()

        if not model.query.filter_by(tx_code=tx_code).first():
            return tx_code

    return generate_tx_code()


def generate_account_reference():
    """Generate a Daraja ``AccountReference``.

    Safaricom limits the account reference to 12 alphanumeric characters, so
    the value is deliberately shorter than a transaction code.
    """
    return f"{TX_CODE_PREFIX}{secrets.token_hex(4).upper()}"


def truncate(value, max_length):
    """Truncate a value so it fits a fixed-length column."""
    if value is None:
        return None

    text = str(value)

    return text[:max_length]

