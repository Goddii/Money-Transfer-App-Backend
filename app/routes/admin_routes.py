from flask import Blueprint, request, jsonify
from sqlalchemy import func
from decimal import Decimal
from app.extensions import db
from app.models import User, Wallet, Transaction
from app.utils.decorators import admin_required

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
        request_data = request.get_json()
        
        full_name = request_data['name'].strip().split(' ', 1)
        new_user = User(
            first_name=full_name[0],
            last_name=full_name[1] if len(full_name) > 1 else '',
            email=request_data['email'],
            phone_number=request_data.get('phone', '') or None,
            status='Active',
            role='user'
        )
        new_user.set_password(request_data.get('password', '123456'))
        
        db.session.add(new_user)
        db.session.commit()

        # Create wallet for user
        initial_balance = request_data.get('initial_balance', 0)
        new_wallet = Wallet(user_id=new_user.id, balance=initial_balance)
        db.session.add(new_wallet)
        db.session.commit()

        return jsonify({"message": "User created successfully", "user_id": new_user.id}), 201

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


# 4. Audit Log
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


# 5. Dynamic Revenue Analytics
@admin_bp.route('/revenue-analytics', methods=['GET'])
@admin_required()
def get_revenue_analytics():
    # Dynamic monthly revenue calculation from real database records
    monthly_trends_query = db.session.query(
        func.to_char(Transaction.timestamp, 'Mon').label('month_name'),
        func.sum(Transaction.fee).label('total_revenue')
    ).group_by(func.to_char(Transaction.timestamp, 'Mon')).all()

    revenue_trend_list = []
    for row in monthly_trends_query:
        revenue_trend_list.append({
            "month": row.month_name,
            "revenue": float(row.total_revenue)
        })

    # Dynamic fee breakdown by transaction type
    source_breakdown_query = db.session.query(
        Transaction.tx_type.label('source_type'),
        func.sum(Transaction.fee).label('source_total')
    ).group_by(Transaction.tx_type).all()

    revenue_source_list = []
    for row in source_breakdown_query:
        revenue_source_list.append({
            "source": row.source_type,
            "amount": "KES " + str(round(row.source_total, 2))
        })

    return jsonify({
        "revenue_trend_months": revenue_trend_list,
        "revenue_by_source": revenue_source_list
    }), 200