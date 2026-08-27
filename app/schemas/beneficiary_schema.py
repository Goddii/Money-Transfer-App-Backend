"""Validation for beneficiary requests."""

from app.utils.errors import ApiError, ErrorCode
from app.utils.validators import (
    normalize_kenyan_phone,
    reject_unexpected_fields,
    require_json_object,
    validate_positive_int,
)

# A beneficiary is identified by a single user-facing account identifier. The
# internal ``beneficiary_user_id`` remains accepted for backwards compatibility
# but the preferred, non-technical options are the recipient's phone number or
# email -- both of which the backend resolves to the internal id internally.
ALLOWED_BENEFICIARY_FIELDS = {"beneficiary_user_id", "phone_number", "email"}

_IDENTIFIER_FIELDS = ("beneficiary_user_id", "phone_number", "email")


def validate_beneficiary_create(data):
    """Validate the payload for creating a beneficiary.

    Exactly one account identifier must be supplied. The owning user is always
    taken from the JWT and can never be supplied by the client.

    Returns a dict with a single key naming the identifier that was provided
    (``beneficiary_user_id``, ``phone_number`` or ``email``), normalised so the
    service layer can resolve it directly.
    """
    require_json_object(data)
    reject_unexpected_fields(data, ALLOWED_BENEFICIARY_FIELDS)

    provided = [
        field
        for field in _IDENTIFIER_FIELDS
        if data.get(field) not in (None, "")
    ]

    if not provided:
        raise ApiError(
            "A phone number or email is required to identify the beneficiary.",
            400,
            ErrorCode.VALIDATION_ERROR,
        )

    if len(provided) > 1:
        raise ApiError(
            "Provide exactly one of phone_number or email (or beneficiary_user_id).",
            400,
            ErrorCode.VALIDATION_ERROR,
        )

    field = provided[0]

    if field == "beneficiary_user_id":
        beneficiary_user_id = validate_positive_int(
            data.get("beneficiary_user_id"), "Beneficiary user id"
        )
        return {"beneficiary_user_id": beneficiary_user_id}

    if field == "phone_number":
        phone_number = normalize_kenyan_phone(data.get("phone_number"))
        return {"phone_number": phone_number}

    # field == "email"
    email = (data.get("email") or "").strip()
    if "@" not in email:
        raise ApiError(
            "A valid email address is required.",
            400,
            ErrorCode.VALIDATION_ERROR,
        )
    return {"email": email}
