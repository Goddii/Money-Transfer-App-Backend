from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from models import db, User

auth_bp = Blueprint('auth', __name__, url_prefix='/api/v1/auth')

# Login Route for both Admin and Customers
@auth_bp.route('/login', methods=['POST'])
def login():
    request_data = request.get_json()

    if not request_data or not request_data.get('email') or not request_data.get('password'):
        return jsonify({"message": "Email and password are required"}), 400

    email = request_data.get('email')
    password = request_data.get('password')

    # Find user by email
    user = User.query.filter_by(email=email).first()

    # Check password
    if not user or not user.check_password(password):
        return jsonify({"message": "Invalid email or password"}), 401

    # Check if user status is active
    if user.status == 'Frozen':
        return jsonify({"message": "Your account has been frozen. Contact admin"}), 403

    # Generate access token containing user id identity
    access_token = create_access_token(identity=user.id)

    return jsonify({
        "message": "Login successful",
        "access_token": access_token,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "status": user.status
        }
    }), 200