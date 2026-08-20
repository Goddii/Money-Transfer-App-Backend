from datetime import datetime
from app.extensions import db


class Transaction(db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    tx_code = db.Column(db.String(20), unique=True, nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    fee = db.Column(db.Numeric(12, 2), default=0.00)
    status = db.Column(db.String(20), default='Completed')
    tx_type = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)