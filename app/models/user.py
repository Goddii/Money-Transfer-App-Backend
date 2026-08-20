from datetime import datetime
from decimal import Decimal
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()
class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    avatar_url = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), default='customer') # 'customer' or 'admin'
    status = db.Column(db.String(20), default='Active') # 'Active' or 'Frozen'
    verification_tier = db.Column(db.String(50), default='VERIFIED CUSTOMER')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship to wallet
    wallet = db.relationship('Wallet', backref='user', uselist=False, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Wallet(db.Model):
    __tablename__ = 'wallets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    balance = db.Column(db.Numeric(12, 2), default=0.00)
    currency = db.Column(db.String(3), default='USD')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)