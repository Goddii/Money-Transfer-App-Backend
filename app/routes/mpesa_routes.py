"""M-Pesa (Safaricom Daraja) endpoints for the normal-user deposit flow."""

from flask import Blueprint, request

from app.schemas.mpesa_schema import parse_stk_callback, validate_stk_push
from app.services.mpesa_service import MpesaService
from app.utils.decorators import get_current_user, handle_api_errors, jwt_required_custom
from app.utils.errors import success_response


mpesa_bp = Blueprint(
    "mpesa",
    __name__,
)


@mpesa_bp.post("/stk-push")
@jwt_required_custom
@handle_api_errors
def initiate_stk_push():
    """Initiate an M-Pesa STK Push deposit for the authenticated user.

    The wallet is not credited here; it is only credited once Safaricom
    confirms the payment through the callback.
    """
    current_user = get_current_user()

    validated_data = validate_stk_push(request.get_json(silent=True) or {})

    mpesa_transaction = MpesaService.initiate_deposit(
        user=current_user,
        amount=validated_data["amount"],
        phone=validated_data["phone"],
    )

    return success_response(
        message="M-Pesa payment request sent. Enter your PIN to complete the deposit.",
        data={"deposit": mpesa_transaction.to_dict()},
        status_code=201,
    )


@mpesa_bp.post("/callback")
@handle_api_errors
def mpesa_callback():
    """Daraja STK Push callback.

    This endpoint is called by Safaricom, not by the Vyloc frontend, so it does
    not require a user JWT. The deposit is identified from the callback's own
    ``CheckoutRequestID``; the payment is reconciled with Daraja (server-side)
    before any wallet is credited, so a forged callback cannot create money.
    """
    # Optional defence-in-depth: reject callbacks from unexpected source IPs
    # when an allowlist is configured. The authoritative protection is the
    # Daraja reconciliation performed inside ``process_callback``.
    MpesaService.reject_unauthorized_source(request)

    parsed_callback = parse_stk_callback(request.get_json(silent=True) or {})

    MpesaService.process_callback(parsed_callback)

    # Always acknowledge a structurally valid callback so Safaricom does not
    # retry. No deposit details are returned to the unauthenticated caller.
    return success_response(message="Callback processed.")
