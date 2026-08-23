from datetime import datetime
from app.extensions import db


class Beneficiary(db.Model):
    __tablename__ = 'beneficiaries'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    beneficiary_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship('User', foreign_keys=[user_id], back_populates='beneficiaries')
    beneficiary = db.relationship('User', foreign_keys=[beneficiary_user_id], back_populates='benefited')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'beneficiary_user_id', name='unique_user_beneficiary'),
    )

    def to_dict(self):
        """Return a safe representation of a saved transfer contact.

        Only the fields required to display and use a beneficiary are exposed;
        internal fields of the beneficiary account (role, status, password
        hash) are never returned.
        """
        beneficiary_user = self.beneficiary

        return {
            'id': self.id,
            'beneficiary_user_id': self.beneficiary_user_id,
            'name': beneficiary_user.name if beneficiary_user else None,
            'first_name': beneficiary_user.first_name if beneficiary_user else None,
            'last_name': beneficiary_user.last_name if beneficiary_user else None,
            'email': beneficiary_user.email if beneficiary_user else None,
            'phone_number': (
                beneficiary_user.phone_number if beneficiary_user else None
            ),
            'avatar_url': beneficiary_user.avatar_url if beneficiary_user else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
