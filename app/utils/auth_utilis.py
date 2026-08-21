from functools import wraps
from flask import jsonify
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity, get_jwt
from app.models import User 


def jwt_required_custom(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            verify_jwt_in_request()
        except Exception as error:
            return jsonify({"success": False, "message": "Authentication required.", "error": str(error)}), 401

        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        if not user:
            return jsonify({"success": False, "message": "User account not found."}), 404

        if user.status != 'Active':
            return jsonify({"success": False, "message": f"Account is {user.status.lower()}."}), 403

        return fn(*args, **kwargs)

    return wrapper


def role_required(required_role):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                verify_jwt_in_request()
            except Exception as error:
                return jsonify({"success": False, "message": "Authentication required.", "error": str(error)}), 401

            current_user_id = get_jwt_identity()
            user = User.query.get(current_user_id)

            if not user:
                return jsonify({"success": False, "message": "User account not found."}), 404

            # Role verification
            if user.role != required_role:
                return jsonify({"success": False, "message": f"Access forbidden. {required_role.capitalize()} role required."}), 403

            # Status verification (Active / Frozen / Inactive)
            if user.status != 'Active':
                return jsonify({"success": False, "message": f"Account is {user.status.lower()}."}), 403

            return fn(*args, **kwargs)

        return wrapper

    return decorator


def admin_required():
    return role_required('admin')