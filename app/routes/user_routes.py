from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.schemas.user_schema import validate_user_update
from app.services.user_service import UserService
from app.utils.decorators import jwt_required_custom


user_bp = Blueprint(
    "users",
    __name__,
)

@user_bp.get("/me")
@jwt_required_custom
def get_current_user():
    user_id = get_jwt_identity()

    user = UserService.get_user_by_id(int(user_id))

    if not user:
        return jsonify(
            {
                "success" : False,
                "message" : "User not found",
            }
        ), 404

    return jsonify(
        {
            "success" : True,
            "data" : {
                "user" : user.to_dict()
            }
        }
    ), 200