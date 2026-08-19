from flask import Blueprint, request, jsonify
from sqlalchemy import func
from decimal import Decimal
from models import db, User, Wallet, Transaction
from utils.auth_utilis import admin_required

admin_bp = Blueprint('admin', __name__, url_prefix='/api/v1/admin')

#1. Admin User List Endpoint (paginated)
@admin_bp.route('/users', methods=['GET'])
@admin_required()  # acts like a guard that safeguards the administrative privileges.
def get_all_users():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)

    pagination = User.query.paginate(page=page, per_page=per_page, error_out=False)
    users = pagination.items

    result = []
    for user in users:
        result.append({
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "wallet_balance": str(user.wallet.balance) if user.wallet else "0.00",
            "created_at": user.created_at.isoformat()
        })
    return jsonify({
        "users": result,
        "total": pagination.total,
        "pages": pagination.pages,
        "current_page": pagination.page
    }), 200

#2. Admin User Details endpoint
@admin_bp.route('/users/<int:user_id>', methods=['GET'])
@admin_required()
def get_user_details(user_id):
    user = User.query.get_or_404(user_id)
    wallet = user.wallet

    return jsonify({
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "wallet": {
            "id": wallet.id,
            "balance": str(wallet.balance),
            "currency": wallet.currency
        } if wallet else None,
        "created_at": user.created_at.isoformat()
    }), 200

#3.Admin Create User endpoint
@admin_bp.route('/users', methods=['POST'])
@admin_required()
def create_user():
    data =request.get_json()
    if User.query.filter_by(email=data.get('email')).first():
        return jsonify({"error": "Email already exists"}), 400
    new_user =User(
        name=data.get('name'),
        email=data.get('email'),
        role=data.get('role', 'customer'),
    )
    new_user.set_password(data.get('password'))
    #Create associated wallet
    new_wallet =Wallet(user=new_user, balance=Decimal(str(data.get('initial_balance', 0.00))))
    db.session.add(new_user)
    db.session.add(new_wallet)
    db.session.commit()

    return jsonify({"message":"User and wallet created successfully", "user_id": new_user.id}), 201
# 4.Admin Update User endpoint
@admin_bp.route('/users/<int:user_id>', methods=['PUT'])
@admin_required()
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json() or{}

    if 'name' in data:
        user.name =data['name']
    if 'role' in data:
        user.role =data['role']
    if 'is_active' in data:
        user.is_active = data['is_active']
    db.session .commit()
    return jsonify({"message": "User updated successfully"}), 200
#5.Admin delete /Deactivate User endpoint
@admin_bp.route('/users/<int:user_id>', methods=['DELETE'])
@admin_required()
def deactivate_user(user_id):
    user =User.query.get_or_404(user_id)
    user.is_active =False
    db.session.commit()
    return jsonify({"message": f"User {user.id} has been deactivated successfully"}), 200

# 6. Admin Wallet Information Endpoint
@admin_bp.route('/wallets', methods=['GET'])
@admin_required()
def get_all_wallets():
    wallets = Wallet.query.all()
    result = [{
        "wallet_id": w.id,
        "user_id": w.user_id,
        "user_name": w.user.name,
        "balance": str(w.balance),
        "currency": w.currency
    } for w in wallets]
    return jsonify({"wallets": result}), 200

