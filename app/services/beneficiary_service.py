"""Beneficiary business logic.

Ownership is always derived from the authenticated user, never from the
request payload.
"""

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.beneficiary import Beneficiary
from app.models.user import User
from app.utils.errors import ApiError, ErrorCode
from app.utils.helpers import is_account_active


class BeneficiaryService:

    @staticmethod
    def list_for_user(user_id):
        return (
            Beneficiary.query.filter_by(user_id=user_id)
            .order_by(Beneficiary.created_at.desc(), Beneficiary.id.desc())
            .all()
        )

    @staticmethod
    def create(owner, beneficiary_user_id):
        if beneficiary_user_id == owner.id:
            raise ApiError(
                "You cannot add yourself as a beneficiary.",
                400,
                ErrorCode.SELF_BENEFICIARY_NOT_ALLOWED,
            )

        beneficiary_user = db.session.get(User, beneficiary_user_id)

        if not beneficiary_user:
            raise ApiError(
                "Beneficiary user not found.", 404, ErrorCode.USER_NOT_FOUND
            )

        if not is_account_active(beneficiary_user):
            raise ApiError(
                "This user cannot be added as a beneficiary.",
                400,
                ErrorCode.INVALID_BENEFICIARY,
            )

        existing = Beneficiary.query.filter_by(
            user_id=owner.id, beneficiary_user_id=beneficiary_user_id
        ).first()

        if existing:
            raise ApiError(
                "This beneficiary has already been added.",
                409,
                ErrorCode.DUPLICATE_BENEFICIARY,
            )

        beneficiary = Beneficiary(
            user_id=owner.id,
            beneficiary_user_id=beneficiary_user_id,
        )

        try:
            db.session.add(beneficiary)
            db.session.commit()
        except IntegrityError:
            # Backstop for the unique_user_beneficiary database constraint.
            db.session.rollback()
            raise ApiError(
                "This beneficiary has already been added.",
                409,
                ErrorCode.DUPLICATE_BENEFICIARY,
            )
        except Exception:
            db.session.rollback()
            raise

        return beneficiary

    @staticmethod
    def get_owned_beneficiary(user_id, beneficiary_id):
        """Load a beneficiary that belongs to ``user_id``.

        A beneficiary owned by another user is reported as not found so record
        existence is not leaked.
        """
        beneficiary = Beneficiary.query.filter_by(
            id=beneficiary_id, user_id=user_id
        ).first()

        if not beneficiary:
            raise ApiError(
                "Beneficiary not found.", 404, ErrorCode.BENEFICIARY_NOT_FOUND
            )

        return beneficiary

    @staticmethod
    def delete(user_id, beneficiary_id):
        beneficiary = BeneficiaryService.get_owned_beneficiary(
            user_id, beneficiary_id
        )

        try:
            db.session.delete(beneficiary)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        return True
