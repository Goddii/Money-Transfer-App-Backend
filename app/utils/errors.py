"""Centralised API response helpers.

The Vyloc API uses a single response envelope (see README "API Response
Convention"):

Success::

    {"success": true, "message": "...", "data": {}}

Error::

    {"success": false, "message": "...", "error": "ERROR_CODE"}

Routes should build every response through :func:`success_response` and
:func:`error_response` so that new endpoints never introduce a competing
response shape, and so internal exception details are never leaked.
"""

from flask import current_app, jsonify, request


class ErrorCode:
    """Stable machine-readable error codes returned in the ``error`` field."""

    # Authentication / authorization
    AUTH_REQUIRED = "AUTH_REQUIRED"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
    FORBIDDEN = "FORBIDDEN"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"

    # Generic request problems
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DUPLICATE_RESOURCE = "DUPLICATE_RESOURCE"

    # Wallet
    WALLET_NOT_FOUND = "WALLET_NOT_FOUND"

    # Beneficiaries
    BENEFICIARY_NOT_FOUND = "BENEFICIARY_NOT_FOUND"
    DUPLICATE_BENEFICIARY = "DUPLICATE_BENEFICIARY"
    SELF_BENEFICIARY_NOT_ALLOWED = "SELF_BENEFICIARY_NOT_ALLOWED"
    INVALID_BENEFICIARY = "INVALID_BENEFICIARY"

    # Transfers / transactions
    INVALID_AMOUNT = "INVALID_AMOUNT"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    RECEIVER_NOT_FOUND = "RECEIVER_NOT_FOUND"
    RECEIVER_NOT_ELIGIBLE = "RECEIVER_NOT_ELIGIBLE"
    SELF_TRANSFER_NOT_ALLOWED = "SELF_TRANSFER_NOT_ALLOWED"
    TRANSACTION_NOT_FOUND = "TRANSACTION_NOT_FOUND"
    TRANSFER_FAILED = "TRANSFER_FAILED"

    # M-Pesa / Daraja
    INVALID_PHONE_NUMBER = "INVALID_PHONE_NUMBER"
    MPESA_NOT_CONFIGURED = "MPESA_NOT_CONFIGURED"
    MPESA_REQUEST_FAILED = "MPESA_REQUEST_FAILED"
    INVALID_CALLBACK_PAYLOAD = "INVALID_CALLBACK_PAYLOAD"


class ApiError(Exception):
    """Error raised by services and translated into an API response.

    Services raise this instead of returning HTTP details so that business
    logic stays independent from the transport layer.
    """

    def __init__(self, message, status_code=400, error_code=ErrorCode.VALIDATION_ERROR):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


def success_response(message=None, data=None, status_code=200):
    """Build a standard successful JSON response."""
    payload = {"success": True}

    if message is not None:
        payload["message"] = message

    if data is not None:
        payload["data"] = data

    return jsonify(payload), status_code


def error_response(message, status_code=400, error_code=ErrorCode.VALIDATION_ERROR):
    """Build a standard error JSON response.

    Only the supplied ``message`` is exposed. Internal exception details,
    stack traces and SQL are never included.
    """
    return jsonify(
        {
            "success": False,
            "message": message,
            "error": error_code,
        }
    ), status_code


def api_error_response(error):
    """Convert an :class:`ApiError` into a standard error response."""
    return error_response(error.message, error.status_code, error.error_code)


def internal_error_response(message="An unexpected error occurred."):
    """Standard 500 response used when an unexpected exception is caught."""
    return error_response(message, 500, ErrorCode.INTERNAL_ERROR)


def log_exception(context):
    """Log an exception server-side without leaking it to the client."""
    current_app.logger.exception("%s failed", context)


def register_error_handlers(app):
    """Register JSON error handlers for the ``/api`` surface.

    Non-API paths keep Flask's default behaviour, so nothing outside the API
    changes.
    """

    def _is_api_request():
        return request.path.startswith("/api")

    @app.errorhandler(404)
    def handle_not_found(error):  # pragma: no cover - exercised via routes
        if not _is_api_request():
            return error
        return error_response("Resource not found.", 404, ErrorCode.NOT_FOUND)

    @app.errorhandler(405)
    def handle_method_not_allowed(error):  # pragma: no cover
        if not _is_api_request():
            return error
        return error_response(
            "Method not allowed.", 405, ErrorCode.METHOD_NOT_ALLOWED
        )

    @app.errorhandler(500)
    def handle_internal_error(error):  # pragma: no cover
        if not _is_api_request():
            return error
        app.logger.exception("Unhandled application error")
        return internal_error_response()
