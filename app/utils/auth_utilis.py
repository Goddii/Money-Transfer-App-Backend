from functools import wraps
from flask import jsonify
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from models import User

def admin_required():
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                # Verify JWT token is present in header
                verify_jwt_in_request()
            except Exception as error:
                return jsonify({"message": "Missing or invalid token", "error": str(error)}), 401

            current_user_id = get_jwt_identity()
            user = User.query.get(current_user_id)

            # Check if user exists
            if not user:
                return jsonify({"message": "User not found"}), 404

            # Check if user is an admin
            if user.role != 'admin':
                return jsonify({"message": "Access denied. Admin role required"}), 403

            # Check if admin account is active
            if user.status != 'Active':
                return jsonify({"message": "Account is frozen or inactive"}), 403

            return func(*args, **kwargs)
        return wrapper
    return decorator