from flask import Blueprint, jsonify, request

from app.schemas.auth_schema import (
    validate_login,
    validate_registration,
)
from app.services.auth_service import AuthService


auth_bp = Blueprint(
  "auth",
  __name__,
)

@auth_bp.post("/register")
def register():
    try:
        data = request.get_json(silent=True) or {}

        validated_data = validate_registration(data)

        user = AuthService.register_user(
           first_name=validated_data["first_name"],
           last_name=validated_data["last_name"],
           email=validated_data["email"],
           password=validated_data["password"],
           phone_number=validated_data["phone_number"] 
        )

        return jsonify(
            {
                "success" : True,
                "message" : "Account created successfully",
                "data" : {
                    "user" : user.to_dict()
                },
            }
        ), 201
    except ValueError as error: 
        return jsonify(
            {
                "success" : False,
                "message" : str(error)
            }
        ), 400

    except Exception:
        return jsonify(
            {
                "success" : False,
                "message" :  "An unexpected error occurred"
            }
        ), 500

@auth_bp.post("/login")
def login():
    try:
        data = request.get_json(silent=True) or {}

        validated_data = validate_login(data)

        user, access_token = AuthService.login_user(
            email=validated_data["email"],
            password=validated_data["password"],
        )

        return jsonify(
            {
                "success": True,
                "message": "Login successful.",
                "data": {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "user": user.to_dict(),
                },
            }
        ), 200

    except ValueError as error:
        return jsonify(
            {
                "success": False,
                "message": str(error),
            }
        ), 401

    except Exception:
        return jsonify(
            {
                "success": False,
                "message": "An unexpected error occurred.",
            }
        ), 500
    