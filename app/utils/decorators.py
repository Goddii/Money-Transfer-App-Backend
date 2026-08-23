from functools import wraps

from flask import g, jsonify
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request

from app.extensions import db
from app.models.user import User
from app.utils.errors import (
    ApiError,
    ErrorCode,
    api_error_response,
    error_response,
    internal_error_response,
    log_exception,
)
from app.utils.helpers import ACTIVE_STATUS, is_account_active


def _load_authenticated_user():
    """Load the user referenced by the verified JWT.

    Returns ``(user, None)`` on success or ``(None, response)`` when the token
    holder is no longer a usable account.
    """
    identity = get_jwt_identity()

    try:
        user_id = int(identity)
    except (TypeError, ValueError):
        return None, error_response(
            "Authentication required.", 401, ErrorCode.AUTH_REQUIRED
        )

    user = db.session.get(User, user_id)

    if not user:
        # The token is validly signed but the account no longer exists, so the
        # credential itself is no longer valid.
        return None, error_response(
            "User account no longer exists.", 401, ErrorCode.USER_NOT_FOUND
        )

    if not is_account_active(user):
        status = (user.status or "inactive").lower()
        return None, error_response(
            f"Account is {status}.", 403, ErrorCode.ACCOUNT_INACTIVE
        )

    g.current_user = user

    return user, None


def jwt_required_custom(fn):
    """Require a valid JWT belonging to an existing, active user.

    The authenticated user is cached on ``flask.g`` and can be retrieved with
    :func:`get_current_user`.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):

        try:
            verify_jwt_in_request()

        except Exception:
            return jsonify(
                {
                    "success": False,
                    "message": "Authentication required.",
                    "error": ErrorCode.AUTH_REQUIRED,
                }
            ), 401

        _, rejection = _load_authenticated_user()

        if rejection:
            return rejection

        return fn(*args, **kwargs)

    return wrapper


def get_current_user():
    """Return the user loaded by :func:`jwt_required_custom`."""
    return getattr(g, "current_user", None)


def handle_api_errors(fn):
    """Translate service/validation failures into the standard error envelope.

    Keeps internal exception details out of API responses while preserving
    server-side logging.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)

        except ApiError as error:
            return api_error_response(error)

        except ValueError as error:
            return error_response(str(error), 400, ErrorCode.VALIDATION_ERROR)

        except Exception:
            log_exception(fn.__name__)
            return internal_error_response()

    return wrapper


def role_required(required_role):

    def decorator(fn):

        @wraps(fn)
        def wrapper(*args, **kwargs):

            try:
                verify_jwt_in_request()

                claims = get_jwt()

                if claims.get("role") != required_role:
                    return jsonify(
                        {
                            "success": False,
                            "message": "Access forbidden.",
                            "error": ErrorCode.FORBIDDEN,
                        }
                    ), 403

            except Exception:
                return jsonify(
                    {
                        "success": False,
                        "message": "Authentication required.",
                        "error": ErrorCode.AUTH_REQUIRED,
                    }
                ), 401

            return fn(*args, **kwargs)

        return wrapper

    return decorator


def admin_required():
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                verify_jwt_in_request()
            except Exception:
                return jsonify({"message": "Authentication required."}), 401

            current_user_id = get_jwt_identity()
            user = User.query.get(current_user_id)

            if not user or user.role != 'admin' or not is_account_active(user):
                return jsonify({"message": "Admin privileges required"}), 403

            return func(*args, **kwargs)
        return wrapped
    return decorator
