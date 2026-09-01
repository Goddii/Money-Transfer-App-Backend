"""M-Pesa (Safaricom Daraja) endpoints for the normal-user deposit flow."""

from flask import Blueprint, request

from app.models.mpesa_transaction import MpesaTransaction
from app.schemas.mpesa_schema import parse_stk_callback, validate_stk_push
from app.services.mpesa_service import MpesaService
from app.utils.decorators import (
    admin_required,
    get_current_user,
    handle_api_errors,
    jwt_required_custom,
)
from app.utils.errors import ApiError, ErrorCode, success_response


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
    confirms the payment through the callback/reconciliation path.
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


@mpesa_bp.get("/transactions/<int:transaction_id>")
@jwt_required_custom
@handle_api_errors
def get_mpesa_transaction_status(transaction_id):
    """Return a user's own M-Pesa deposit status.

    JWT protected and ownership-scoped: a user may only retrieve their own
    deposit, identified by the internal primary-key id (never the Daraja
    ``checkout_request_id``). Other users' deposits are reported as not found so
    they are never exposed. The phone number is masked and no Daraja credentials
    or internal reconciliation text are returned.
    """
    current_user = get_current_user()

    mpesa_transaction = MpesaTransaction.query.filter_by(
        id=transaction_id, user_id=current_user.id
    ).first()

    if not mpesa_transaction:
        raise ApiError(
            "M-Pesa transaction not found.",
            404,
            ErrorCode.TRANSACTION_NOT_FOUND,
        )

    return success_response(
        message="M-Pesa transaction status.",
        data={"transaction": mpesa_transaction.to_status_dict()},
    )


@mpesa_bp.post("/transactions/<int:transaction_id>/reconcile")
@jwt_required_custom
@handle_api_errors
def reconcile_user_deposit(transaction_id):
    """User-scoped nudge to recover the caller's own stuck deposit.

    The backend automatically reconciles (see the background sweep started in
    ``create_app``), but this lets the frontend actively trigger recovery of a
    PENDING / RECONCILIATION_PENDING deposit without waiting for the next sweep
    or an admin. Ownership-scoped: a user can only reconcile their own deposit,
    identified by the internal primary-key id, never the Daraja
    ``checkout_request_id``. The Daraja reconciliation that actually authorises a
    credit still runs server-side, so this can never manufacture money.
    """
    current_user = get_current_user()

    status = MpesaService.reconcile_user_deposit(current_user.id, transaction_id)

    return success_response(
        message="M-Pesa deposit reconciliation attempted.",
        data={"status": status},
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


@mpesa_bp.post("/admin/reconcile")
@admin_required()
@handle_api_errors
def admin_reconcile_deposits():
    """Admin-triggered recovery of unresolved M-Pesa deposits.

    Runs the idempotent recovery service over PENDING and RECONCILIATION_PENDING
    deposits. Safe to invoke repeatedly; a deposit already COMPLETED or FAILED
    is never re-credited or flipped. Returns only aggregate counts and never
    other users' sensitive data.
    """
    summary = MpesaService.recover_deposits()

    return success_response(
        message="M-Pesa deposit reconciliation completed.",
        data={"summary": summary},
    )
