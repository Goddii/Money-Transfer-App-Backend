from  functools import wraps
from flask import jsonify 
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_jwt_extended
from models import User

def admin_required():
    def decorator(func):
        @wraps(func)
        def decorated_function(*args, **kwargs):
            verify_jwt_in_jwt_extended()
            current_user_id = get_jwt_identity()
            user = User.query.get(current_user_id)
            if not user or user.role != 'admin' or not user.is_active:
                return jsonify({"error": "Admin privileges required"}), 403
            return func(*args, **kwargs)
        return decorated_function
    return decorator
            
