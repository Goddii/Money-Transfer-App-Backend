"""Service payment endpoints.

Provides CRUD for simulated service payments (Electricity, Water, Airtime).
All endpoints require JWT authentication and verify ownership.
"""

from flask import Blueprint, request

from app.schemas.service_payment_schema import (
    validate_history_query,
    validate_service_payment,
)
from app.services.service_payment_service import ServicePaymentService
from app.utils.decorators import (
    get_current_user,
    handle_api_errors,
    jwt_required_custom,
)
from app.utils.errors import success_response

service_payment_bp = Blueprint(
    "service_payments",
    __name__,
)


@service_payment_bp.get("/services")
@jwt_required_custom
@handle_api_errors
def list_services():
    """Return available service providers."""
    services = ServicePaymentService.list_services()

    return success_response(
        message="Services retrieved successfully.",
        data={"services": services},
    )


@service_payment_bp.post("/service-payments")
@jwt_required_custom
@handle_api_errors
def create_service_payment():
    """Initiate a service payment from the authenticated user's wallet."""
    current_user = get_current_user()

    validated_data = validate_service_payment(request.get_json(silent=True) or {})

    service_payment = ServicePaymentService.initiate_payment(
        user=current_user,
        service_type=validated_data["service_type"],
        account_number=validated_data["account_number"],
        amount=validated_data["amount"],
        idempotency_key=validated_data.get("idempotency_key"),
    )

    return success_response(
        message="Service payment initiated successfully.",
        data={"payment": service_payment.to_dict()},
        status_code=201,
    )


@service_payment_bp.get("/service-payments")
@jwt_required_custom
@handle_api_errors
def list_service_payments():
    """List service payments for the authenticated user."""
    current_user = get_current_user()

    pagination_args = validate_history_query(request.args)

    pagination = ServicePaymentService.list_for_user(
        current_user.id,
        page=pagination_args["page"],
        per_page=pagination_args["per_page"],
    )

    return success_response(
        message="Service payments retrieved successfully.",
        data={
            "payments": [p.to_dict() for p in pagination.items],
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


@service_payment_bp.get("/service-payments/<int:payment_id>")
@jwt_required_custom
@handle_api_errors
def get_service_payment(payment_id):
    """Return a single service payment owned by the authenticated user."""
    current_user = get_current_user()

    service_payment = ServicePaymentService.get_for_user(payment_id, current_user.id)

    return success_response(
        message="Service payment retrieved successfully.",
        data={"payment": service_payment.to_dict()},
    )


@service_payment_bp.post("/service-payments/<int:payment_id>/reconcile")
@jwt_required_custom
@handle_api_errors
def reconcile_service_payment(payment_id):
    """Reconcile a pending service payment.

    Re-runs the simulated provider to resolve the payment to a final state.
    Idempotent: calling on a terminal payment is a no-op.
    """
    current_user = get_current_user()

    service_payment = ServicePaymentService.reconcile_payment(
        current_user.id, payment_id
    )

    return success_response(
        message="Service payment reconciliation completed.",
        data={"payment": service_payment.to_dict()},
    )
