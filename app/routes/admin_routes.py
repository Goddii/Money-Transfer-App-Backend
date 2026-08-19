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