"""Validation for service payment requests."""

from app.models.service_payment import ServiceType
from app.utils.errors import ApiError, ErrorCode
from app.utils.validators import (
    reject_unexpected_fields,
    require_json_object,
    validate_money_amount,
    validate_positive_int,
)

ALLOWED_PAYMENT_FIELDS = {"service_type", "account_number", "amount", "idempotency_key"}

DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100


def validate_service_payment(data):
    """Validate a service payment request.

    Returns cleaned data: {service_type, account_number, amount}.
    """
    require_json_object(data)
    reject_unexpected_fields(data, ALLOWED_PAYMENT_FIELDS)

    if "service_type" not in data:
        raise ApiError("Service type is required.", 400, ErrorCode.VALIDATION_ERROR)

    service_type = (data.get("service_type") or "").strip().upper()

    if service_type not in ServiceType.ALL:
        raise ApiError(
            f"Invalid service type. Must be one of: {', '.join(ServiceType.ALL)}",
            400,
            ErrorCode.INVALID_SERVICE_TYPE,
        )

    if "account_number" not in data:
        raise ApiError(
            "Account number is required.", 400, ErrorCode.INVALID_ACCOUNT_NUMBER
        )

    account_number = data.get("account_number")

    if not account_number or not isinstance(account_number, str):
        raise ApiError(
            "Account number must be a string.", 400, ErrorCode.INVALID_ACCOUNT_NUMBER
        )

    account_number = account_number.strip()

    if not account_number:
        raise ApiError(
            "Account number is required.", 400, ErrorCode.INVALID_ACCOUNT_NUMBER
        )

    if "amount" not in data:
        raise ApiError("Amount is required.", 400, ErrorCode.INVALID_AMOUNT)

    amount = validate_money_amount(data.get("amount"))

    idempotency_key = data.get("idempotency_key")
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str):
            raise ApiError(
                "idempotency_key must be a string.", 400, ErrorCode.VALIDATION_ERROR
            )
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            idempotency_key = None
        elif len(idempotency_key) > 64:
            raise ApiError(
                "idempotency_key is too long (max 64 characters).",
                400,
                ErrorCode.VALIDATION_ERROR,
            )

    return {
        "service_type": service_type,
        "account_number": account_number,
        "amount": amount,
        "idempotency_key": idempotency_key,
    }


def validate_history_query(args):
    """Validate pagination arguments for service payment history."""
    page = args.get("page", 1)
    per_page = args.get("per_page", DEFAULT_PER_PAGE)

    page = validate_positive_int(page, "Page")
    per_page = validate_positive_int(per_page, "Per page")

    if per_page > MAX_PER_PAGE:
        per_page = MAX_PER_PAGE

    return {"page": page, "per_page": per_page}
