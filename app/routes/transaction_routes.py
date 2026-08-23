"""Normal-user transaction endpoints."""

from flask import Blueprint, request

from app.schemas.transaction_schema import validate_history_query, validate_transfer
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService
from app.utils.decorators import get_current_user, handle_api_errors, jwt_required_custom
from app.utils.errors import success_response


transaction_bp = Blueprint(
    "transactions",
    __name__,
)


@transaction_bp.post("/transfer")
@jwt_required_custom
@handle_api_errors
def create_transfer():
    """Send money from the authenticated user's wallet to another user."""
    current_user = get_current_user()

    validated_data = validate_transfer(request.get_json(silent=True) or {})

    transaction = TransactionService.transfer(
        sender=current_user,
        receiver_id=validated_data["receiver_id"],
        amount=validated_data["amount"],
        note=validated_data["note"],
    )

    wallet = WalletService.get_wallet_by_user_id(current_user.id)

    return success_response(
        message="Transfer completed successfully.",
        data={
            "transaction": transaction.to_dict(current_user_id=current_user.id),
            "wallet": wallet.to_dict() if wallet else None,
        },
        status_code=201,
    )


@transaction_bp.get("")
@jwt_required_custom
@handle_api_errors
def list_transactions():
    """List transactions involving the authenticated user."""
    current_user = get_current_user()

    pagination_args = validate_history_query(request.args)

    pagination = TransactionService.list_for_user(
        current_user.id,
        page=pagination_args["page"],
        per_page=pagination_args["per_page"],
    )

    return success_response(
        message="Transactions retrieved successfully.",
        data={
            "transactions": [
                transaction.to_dict(current_user_id=current_user.id)
                for transaction in pagination.items
            ],
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "pages": pagination.pages,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev,
            },
        },
    )


@transaction_bp.get("/<int:transaction_id>")
@jwt_required_custom
@handle_api_errors
def get_transaction(transaction_id):
    """Return a single transaction the authenticated user participated in."""
    current_user = get_current_user()

    transaction = TransactionService.get_for_user(transaction_id, current_user.id)

    return success_response(
        message="Transaction retrieved successfully.",
        data={"transaction": transaction.to_dict(current_user_id=current_user.id)},
    )
