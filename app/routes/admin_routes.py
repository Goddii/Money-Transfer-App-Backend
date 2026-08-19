from flask import Blueprint, request, jsonify
from sqlalchemy import func
from decimal import Decimal
from models import db, User, Wallet, Transaction

admin_bp = Blueprint('admin', __name__ ,url_prefix ='api/v1/admin')

#1. Admin User List Endpoint (paginated)
@admin_bp .route('/users', methods=['GET'])
# @admin_required ()# acts like a guard  that safeguards the administrative privileges.