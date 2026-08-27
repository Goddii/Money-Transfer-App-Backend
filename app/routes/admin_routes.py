from flask import Blueprint, request, jsonify
from sqlalchemy import func, or_
from decimal import Decimal
from datetime import datetime

from app.extensions import db
from app.models import User, Wallet, Transaction
from app.models.transaction import TransactionType
from app.schemas.user_schema import validate_admin_user_create, validate_admin_user_update
from app.services.analytics_service import AnalyticsService
from app.services.transaction_service import TransactionService
from app.services.user_service import UserService
from app.utils.decorators import admin_required
from app.utils.errors import ApiError, ErrorCode, error_response


admin_bp = Blueprint('admin', __name__, url_prefix='/api/v1/admin')

# 1. Dashboard Overview
@admin_bp.route('/overview', methods=['GET'])
@admin_required()
def get_dashboard_overview():
    total_users = User.query.count()
    active_wallets = Wallet.query.filter(Wallet.balance > 0).count()

    # Calculate totals directly from the database
    total_liquidity = db.session.query(func.sum(Wallet.balance)).scalar() or 0
    collected_fees = db.session.query(func.sum(Transaction.fee)).scalar() or 0

    return jsonify({
        "total_users": str(total_users),
        "active_wallets": str(active_wallets),
        "platform_liquidity": "KES " + str(round(total_liquidity, 2)),
        "collected_fees": "KES " + str(round(collected_fees, 2))
    }), 200

# 2. Get Users and Create User
@admin_bp.route('/users', methods=['GET', 'POST'])
@admin_required()
def handle_users():
    if request.method == 'POST':
        try:
            request_data = request.get_json(silent=True) or {}

            validated = validate_admin_user_create(request_data)

            if User.query.filter_by(email=validated["email"]).first():
                return error_response(
                    "A user with this email already exists.",
                    409,
                    ErrorCode.DUPLICATE_RESOURCE,
                )

            if (
                validated["phone_number"]
                and User.query.filter_by(
                    phone_number=validated["phone_number"]
                ).first()
            ):
                return error_response(
                    "A user with this phone number already exists.",
                    409,
                    ErrorCode.DUPLICATE_RESOURCE,
                )

            new_user = User(
                first_name=validated["first_name"],
                last_name=validated["last_name"],
                email=validated["email"],
                phone_number=validated["phone_number"],
                status='Active',
                role='user'
            )
            new_user.set_password(validated["password"])

            db.session.add(new_user)
            db.session.flush()

            # Create wallet for user
            new_wallet = Wallet(
                user_id=new_user.id, balance=validated["initial_balance"]
            )
            db.session.add(new_wallet)
            db.session.commit()

            return jsonify({
                "message": "User created successfully",
                "user_id": new_user.id
            }), 201

        except ApiError as error:
            return error_response(error.message, error.status_code, error.error_code)

        except ValueError as error:
            return error_response(str(error), 400, ErrorCode.VALIDATION_ERROR)

        except Exception:
            db.session.rollback()
            return error_response(
                "An unexpected error occurred.", 500, ErrorCode.INTERNAL_ERROR
            )

    # GET Users List
    status_filter = request.args.get('status')
    if status_filter and status_filter != 'All':
        users_list = User.query.filter_by(status=status_filter).all()
    else:
        users_list = User.query.all()

    formatted_users = []
    for user in users_list:
        wallet = Wallet.query.filter_by(user_id=user.id).first()
        user_balance = "0.00"
        if wallet:
            user_balance = str(wallet.balance)

        formatted_users.append({
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "status": user.status,
            "wallet_balance": user_balance
        })

    return jsonify({"users": formatted_users}), 200

# 3. User Profile and Freeze Toggle
@admin_bp.route('/users/<int:user_id>/<string:action>', methods=['GET', 'PATCH'])
@admin_required()
def handle_user_action(user_id, action):
    user = User.query.get_or_404(user_id)

    if action == 'toggle-freeze':
        if user.status == 'Active':
            user.status = 'Frozen'
        else:
            user.status = 'Active'
        db.session.commit()
        return jsonify({"message": "Status updated", "status": user.status}), 200

    if action == 'profile':
        wallet = Wallet.query.filter_by(user_id=user.id).first()

        # Dynamic totals calculated from database
        total_sent = db.session.query(func.sum(Transaction.amount)).filter_by(sender_id=user.id).scalar() or 0
        total_received = db.session.query(func.sum(Transaction.amount)).filter_by(receiver_id=user.id).scalar() or 0

        return jsonify({
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "status": user.status,
            "phone": user.phone_number if user.phone_number else "+1 (555) 000-0000",
            "wallet_balance": "KES " + str(round(wallet.balance, 2)) if wallet else "KES 0.00",
            "total_sent": "KES " + str(round(total_sent, 2)),
            "total_received": "KES " + str(round(total_received, 2))
        }), 200

    return error_response("Unknown user action", 404, ErrorCode.NOT_FOUND)


# 4. Admin update user
@admin_bp.route('/users/<int:user_id>', methods=['PATCH'])
@admin_required()
def update_user(user_id):
    """Update permitted user fields (name, phone, status)."""

    user = User.query.get_or_404(user_id)

    try:
        request_data = request.get_json(silent=True) or {}
        validated = validate_admin_user_update(request_data)
    except ValueError as error:
        return error_response(str(error), 400, ErrorCode.VALIDATION_ERROR)

    try:
        updated = UserService.update_user_admin(user, **validated)
    except ValueError as error:
        return error_response(str(error), 400, ErrorCode.VALIDATION_ERROR)

    return jsonify({
        "message": "User updated successfully",
        "user": updated.to_dict(),
    }), 200


# 5. Admin safe delete / deactivate
@admin_bp.route('/users/<int:user_id>', methods=['DELETE'])
@admin_required()
def delete_user(user_id):
    """Delete a user only when they have no financial history.

    Accounts that participated in transfers or M-Pesa deposits carry an audit
    trail that must be preserved, so deletion is refused with ``409`` and the
    admin is told to freeze/deactivate the account instead.
    """

    user = User.query.get_or_404(user_id)

    if UserService.has_financial_history(user.id):
        return error_response(
            "This account has financial history (transactions or M-Pesa "
            "records) and cannot be deleted. Freeze or deactivate the account "
            "instead to preserve the audit trail.",
            409,
            ErrorCode.VALIDATION_ERROR,
        )

    UserService.delete_user(user)

    return jsonify({
        "message": "User deleted successfully",
        "user_id": user_id,
    }), 200


# 6. Audit Log
@admin_bp.route('/audit-log', methods=['GET'])
@admin_required()
def get_audit_log():
    status_filter = request.args.get('status')
    if status_filter and status_filter != 'All':
        transactions = Transaction.query.filter_by(status=status_filter).order_by(Transaction.timestamp.desc()).all()
    else:
        transactions = Transaction.query.order_by(Transaction.timestamp.desc()).all()

    audit_records = []
    for tx in transactions:
        sender_user = User.query.get(tx.sender_id) if tx.sender_id else None
        receiver_user = User.query.get(tx.receiver_id) if tx.receiver_id else None

        audit_records.append({
            "tx_code": tx.tx_code,
            "status": tx.status,
            "sender_name": sender_user.name if sender_user else "External Bank",
            "receiver_name": receiver_user.name if receiver_user else "System",
            "amount": "KES " + str(tx.amount),
            "fee": "KES " + str(tx.fee),
            "timestamp": tx.timestamp.strftime("%Y-%m-%d %H:%M")
        })

    return jsonify({"audit_log": audit_records}), 200


# 7. Admin per-user transaction history
@admin_bp.route('/users/<int:user_id>/transactions', methods=['GET'])
@admin_required()
def get_user_transactions(user_id):
    """Return only the requested user's transactions (admin scoped)."""

    user = User.query.get_or_404(user_id)

    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
    except (TypeError, ValueError):
        page, per_page = 1, 20

    if page < 1:
        page = 1
    if per_page < 1:
        per_page = 20

    pagination = TransactionService.list_for_user(
        user.id, page=page, per_page=per_page
    )

    return jsonify({
        "user_id": user.id,
        "transactions": [
            transaction.to_dict()
            for transaction in pagination.items
        ],
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        },
    }), 200


# 8. Platform wallet analytics
@admin_bp.route('/platform', methods=['GET'])
@admin_required()
def get_platform_analytics():
    """Return platform-wide analytics used by the admin Platform Stats page."""

    analytics = AnalyticsService.platform_analytics()

    return jsonify(analytics), 200


# 9. Dynamic Revenue Analytics (database-agnostic)
@admin_bp.route('/revenue-analytics', methods=['GET'])
@admin_required()
def get_revenue_analytics():
    # Aggregate fees in Python so the same logic runs on SQLite and PostgreSQL
    # without dialect-specific functions (no PostgreSQL to_char).
    transactions = Transaction.query.order_by(Transaction.timestamp.asc()).all()

    monthly = {}
    by_source = {}

    for tx in transactions:
        if tx.fee is None:
            continue

        if tx.timestamp:
            key = (tx.timestamp.year, tx.timestamp.month)
            monthly.setdefault(key, 0.0)
            monthly[key] += float(tx.fee)

        source = tx.tx_type or "Other"
        by_source[source] = by_source.get(source, 0.0) + float(tx.fee)

    revenue_trend_list = []
    for key in sorted(monthly):
        year, month = key
        revenue_trend_list.append({
            "month": datetime(year, month, 1).strftime("%b"),
            "revenue": round(monthly[key], 2)
        })

    revenue_source_list = [
        {"source": source, "amount": "KES " + str(round(total, 2))}
        for source, total in by_source.items()
    ]

    return jsonify({
        "revenue_trend_months": revenue_trend_list,
        "revenue_by_source": revenue_source_list
    }), 200
