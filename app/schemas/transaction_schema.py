"""Validation for transaction requests."""

from app.utils.errors import ApiError, ErrorCode
from app.utils.validators import (
    reject_unexpected_fields,
    require_json_object,
    validate_money_amount,
    validate_optional_text,
    validate_positive_int,
)

NOTE_MAX_LENGTH = 255
DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100

ALLOWED_TRANSFER_FIELDS = {"receiver_id", "amount", "note"}


def validate_transfer(data):
    """Validate a peer-to-peer transfer request.

    The sender is never read from the payload: it always comes from the JWT.
    """
    require_json_object(data)
    reject_unexpected_fields(data, ALLOWED_TRANSFER_FIELDS)

    if "receiver_id" not in data:
        raise ApiError("Receiver id is required.", 400, ErrorCode.VALIDATION_ERROR)

    if "amount" not in data:
        raise ApiError("Amount is required.", 400, ErrorCode.INVALID_AMOUNT)

    return {
        "receiver_id": validate_positive_int(data.get("receiver_id"), "Receiver id"),
        "amount": validate_money_amount(data.get("amount")),
        "note": validate_optional_text(data.get("note"), "Note", NOTE_MAX_LENGTH),
    }


def validate_history_query(args):
    """Validate pagination arguments for the transaction history endpoint."""
    page = args.get("page", 1)
    per_page = args.get("per_page", DEFAULT_PER_PAGE)

    page = validate_positive_int(page, "Page")
    per_page = validate_positive_int(per_page, "Per page")

    if per_page > MAX_PER_PAGE:
        per_page = MAX_PER_PAGE

    return {"page": page, "per_page": per_page}
