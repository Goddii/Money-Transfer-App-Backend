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