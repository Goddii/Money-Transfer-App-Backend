"""Validation for beneficiary requests."""

from app.utils.errors import ApiError, ErrorCode
from app.utils.validators import (
    reject_unexpected_fields,
    require_json_object,
    validate_positive_int,
)

ALLOWED_BENEFICIARY_FIELDS = {"beneficiary_user_id"}


def validate_beneficiary_create(data):
    """Validate the payload for creating a beneficiary.

    Only ``beneficiary_user_id`` is accepted; the owning user is always taken
    from the JWT and can never be supplied by the client.
    """
    require_json_object(data)
    reject_unexpected_fields(data, ALLOWED_BENEFICIARY_FIELDS)

    if "beneficiary_user_id" not in data:
        raise ApiError(
            "Beneficiary user id is required.", 400, ErrorCode.VALIDATION_ERROR
        )

    beneficiary_user_id = validate_positive_int(
        data.get("beneficiary_user_id"), "Beneficiary user id"
    )

    return {"beneficiary_user_id": beneficiary_user_id}
