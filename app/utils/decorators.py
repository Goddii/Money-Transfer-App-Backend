from functools import wraps

from flask import jsonify
from flask_jwt_extended import get_jwt, verify_jwt_in_request


def jwt_required_custom(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        try:
            verify_jwt_in_request()

        except Exception:
            return jsonify(
                {
                    "success": False,
                    "message": "Authentication required.",
                }
            ), 401

        return fn(*args, **kwargs)

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
                        }
                    ), 403

            except Exception:
                return jsonify(
                    {
                        "success": False,
                        "message": "Authentication required.",
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

            if not user or user.role != 'admin' or user.status != 'Active':
                return jsonify({"message": "Admin privileges required"}), 403

            return func(*args, **kwargs)
        return wrapped
    return decorator