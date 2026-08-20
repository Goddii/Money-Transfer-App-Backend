from datetime import datetime
from app.extensions import db


class Beneficiary(db.Model):
    __tablename__ = 'beneficiaries'

    id = db.Column(db.Integer, primary_key=True),
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False),
    beneficiary_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False),
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship('User', foreign_keys=[user_id], back_populates='beneficiaries')
    beneficiary = db.relationship('User', foreign_keys=[beneficiary_user_id], back_populates='benefited')

    _table_args__ = (
        db.UniqueConstraint('user_id', 'beneficiary_user_id', name='unique_user_beneficiary'),
    )