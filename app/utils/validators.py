"""Reusable request-value validators.

Validators raise :class:`~app.utils.errors.ApiError` so that routes can
translate a failure into the standard API error envelope without duplicating
error handling.
"""

import re
from decimal import Decimal, InvalidOperation

from app.utils.errors import ApiError, ErrorCode
from app.utils.helpers import MONEY_MAX, ZERO_MONEY

# Safaricom expects the MSISDN in the ``2547XXXXXXXX`` / ``2541XXXXXXXX`` form.
KENYAN_MSISDN_PATTERN = re.compile(r"^254(7|1)\d{8}$")
NON_DIGIT_PATTERN = re.compile(r"[^\d+]")


def require_json_object(data):
    """Ensure the request body is a JSON object, raising otherwise."""
    if not isinstance(data, dict):
        raise ApiError(
            "Request body must be a JSON object.", 400, ErrorCode.VALIDATION_ERROR
        )


def reject_unexpected_fields(data, allowed_fields):
    """Reject any field not present in ``allowed_fields``.

    ``allowed_fields`` may be a set or sequence of permitted keys.
    """
    unexpected = set(data.keys()) - set(allowed_fields)
    if unexpected:
        raise ApiError(
            "Invalid fields: " + ", ".join(sorted(unexpected)),
            400,
            ErrorCode.VALIDATION_ERROR,
        )


def validate_positive_int(value, field_name):
    """Validate that ``value`` is a positive integer identifier."""
    if isinstance(value, bool) or value is None:
        raise ApiError(f"{field_name} is required.", 400, ErrorCode.VALIDATION_ERROR)

    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ApiError(
            f"{field_name} must be a valid integer.", 400, ErrorCode.VALIDATION_ERROR
        )

    if parsed <= 0:
        raise ApiError(
            f"{field_name} must be a positive integer.",
            400,
            ErrorCode.VALIDATION_ERROR,
        )

    return parsed


def validate_money_amount(value, field_name="Amount"):
    """Validate and normalise a monetary amount.

    Returns a :class:`~decimal.Decimal` with at most two decimal places.
    Floating-point arithmetic is never used.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ApiError(f"{field_name} is required.", 400, ErrorCode.INVALID_AMOUNT)

    if isinstance(value, bool):
        raise ApiError(
            f"{field_name} must be a valid number.", 400, ErrorCode.INVALID_AMOUNT
        )

    if isinstance(value, float):
        # Accepted for convenience but converted through ``str`` so the decimal
        # literal the client sent is preserved.
        value = repr(value)

    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        raise ApiError(
            f"{field_name} must be a valid number.", 400, ErrorCode.INVALID_AMOUNT
        )

    if not amount.is_finite():
        raise ApiError(
            f"{field_name} must be a valid number.", 400, ErrorCode.INVALID_AMOUNT
        )

    if amount <= ZERO_MONEY:
        raise ApiError(
            f"{field_name} must be greater than zero.", 400, ErrorCode.INVALID_AMOUNT
        )

    if amount.as_tuple().exponent < -2:
        raise ApiError(
            f"{field_name} cannot have more than 2 decimal places.",
            400,
            ErrorCode.INVALID_AMOUNT,
        )

    if amount > MONEY_MAX:
        raise ApiError(
            f"{field_name} exceeds the maximum supported value.",
            400,
            ErrorCode.INVALID_AMOUNT,
        )

    return amount.quantize(Decimal("0.01"))


def validate_optional_text(value, field_name, max_length):
    """Validate an optional free-text field such as a transfer note."""
    if value is None:
        return None

    if not isinstance(value, str):
        raise ApiError(
            f"{field_name} must be a string.", 400, ErrorCode.VALIDATION_ERROR
        )

    text = value.strip()

    if not text:
        return None

    if len(text) > max_length:
        raise ApiError(
            f"{field_name} cannot exceed {max_length} characters.",
            400,
            ErrorCode.VALIDATION_ERROR,
        )

    return text


def normalize_kenyan_phone(value, field_name="Phone number"):
    """Normalise a Kenyan mobile number to the Daraja ``2547XXXXXXXX`` format.

    Accepted inputs: ``07XXXXXXXX``, ``01XXXXXXXX``, ``7XXXXXXXX``,
    ``1XXXXXXXX``, ``2547XXXXXXXX``, ``+2547XXXXXXXX`` (with optional spaces,
    dashes or brackets).
    """
    if not value or not isinstance(value, str):
        raise ApiError(
            f"{field_name} is required.", 400, ErrorCode.INVALID_PHONE_NUMBER
        )

    cleaned = NON_DIGIT_PATTERN.sub("", value.strip()).lstrip("+")

    if cleaned.startswith("0"):
        cleaned = f"254{cleaned[1:]}"
    elif len(cleaned) == 9 and cleaned[0] in ("7", "1"):
        cleaned = f"254{cleaned}"

    if not KENYAN_MSISDN_PATTERN.match(cleaned):
        raise ApiError(
            f"{field_name} must be a valid Kenyan mobile number.",
            400,
            ErrorCode.INVALID_PHONE_NUMBER,
        )

    return cleaned
