from flask_jwt_extended import create_access_token
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.user import User
from app.services.wallet_service import WalletService
from app.utils.errors import ApiError, ErrorCode
from app.utils.helpers import is_account_active


class AuthService:

    @staticmethod
    def register_user(first_name, last_name, email, phone_number, password):
        email = email.strip().lower()
        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            raise ApiError("Email already exists", 409, ErrorCode.DUPLICATE_RESOURCE)

        if phone_number:
            phone_number = phone_number.strip()
            existing_phone = User.query.filter_by(phone_number=phone_number).first()
            if existing_phone:
                raise ApiError(
                    "Phone number already exists", 409, ErrorCode.DUPLICATE_RESOURCE
                )

        user = User(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email,
            phone_number=phone_number.strip() if phone_number else None,
            role='user'
        )
        user.set_password(password)

        try:
            db.session.add(user)
            # Flush (not commit) so the generated user id is available for the
            # wallet while both rows stay inside one transaction.
            db.session.flush()

            WalletService.create_wallet(user.id)

            db.session.commit()
        except IntegrityError:
            # Unique constraints (email, phone number, one wallet per user).
            db.session.rollback()
            raise ApiError(
                "Account could not be created with the details provided",
                409,
                ErrorCode.DUPLICATE_RESOURCE,
            )
        except Exception:
            db.session.rollback()
            raise

        return user

    @staticmethod
    def login_user(email, password):
        email = email.strip().lower()
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            raise ValueError("Invalid email or password")
        if not is_account_active(user):
            status = (user.status or "inactive").lower()
            raise ValueError(f"User account is {status}")
        access_token = create_access_token(identity=str(user.id), additional_claims={"role": user.role})
        return user, access_token
