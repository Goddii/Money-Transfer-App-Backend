"""Normal-user wallet endpoints."""

from flask import Blueprint

from app.services.wallet_service import WalletService
from app.utils.decorators import get_current_user, handle_api_errors, jwt_required_custom
from app.utils.errors import success_response


wallet_bp = Blueprint(
    "wallet",
    __name__,
)


@wallet_bp.get("")
@jwt_required_custom
@handle_api_errors
def get_my_wallet():
    """Return the authenticated user's wallet.

    The owner is taken exclusively from the JWT; the client cannot request
    another user's wallet.
    """
    current_user = get_current_user()

    wallet = WalletService.get_wallet_or_error(
        current_user.id, message="Wallet not found for this account."
    )

    return success_response(
        message="Wallet retrieved successfully.",
        data={"wallet": wallet.to_dict()},
    )
