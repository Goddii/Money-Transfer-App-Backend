from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.user import User


class AuthService:

    @staticmethod
    def register_user(first_name, last_name, email, phone_number, password):
        email = email.strip().lower()
        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            raise ValueError("Email already exists")

        if phone_number:
            phone_number = phone_number.strip()
            existing_phone = User.query.filter_by(phone_number=phone_number).first()
            if existing_phone:
                raise ValueError("Phone number already exists")

        user = User(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email,
            phone_number=phone_number.strip() if phone_number else None,
            role='user'
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

    @staticmethod
    def login_user(email, password):
        email = email.strip().lower()
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            raise ValueError("Invalid email or password")
        if not user.is_active:
            raise ValueError("User account is inactive")
        access_token = create_access_token(identity=str(user.id), additional_claims={"role": user.role})
        return user, access_token
