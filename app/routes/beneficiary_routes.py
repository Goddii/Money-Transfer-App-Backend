"""Normal-user beneficiary endpoints."""

from flask import Blueprint, request

from app.schemas.beneficiary_schema import validate_beneficiary_create
from app.services.beneficiary_service import BeneficiaryService
from app.utils.decorators import get_current_user, handle_api_errors, jwt_required_custom
from app.utils.errors import success_response


beneficiary_bp = Blueprint(
    "beneficiaries",
    __name__,
)


@beneficiary_bp.get("")
@jwt_required_custom
@handle_api_errors
def list_beneficiaries():
    """List the authenticated user's beneficiaries only."""
    current_user = get_current_user()

    beneficiaries = BeneficiaryService.list_for_user(current_user.id)

    return success_response(
        message="Beneficiaries retrieved successfully.",
        data={
            "beneficiaries": [
                beneficiary.to_dict() for beneficiary in beneficiaries
            ]
        },
    )


@beneficiary_bp.post("")
@jwt_required_custom
@handle_api_errors
def create_beneficiary():
    """Add a beneficiary for the authenticated user."""
    current_user = get_current_user()

    validated_data = validate_beneficiary_create(request.get_json(silent=True) or {})

    beneficiary = BeneficiaryService.create(
        owner=current_user,
        beneficiary_user_id=validated_data["beneficiary_user_id"],
    )

    return success_response(
        message="Beneficiary added successfully.",
        data={"beneficiary": beneficiary.to_dict()},
        status_code=201,
    )


@beneficiary_bp.delete("/<int:beneficiary_id>")
@jwt_required_custom
@handle_api_errors
def delete_beneficiary(beneficiary_id):
    """Remove one of the authenticated user's beneficiaries."""
    current_user = get_current_user()

    BeneficiaryService.delete(current_user.id, beneficiary_id)

    return success_response(message="Beneficiary removed successfully.")
