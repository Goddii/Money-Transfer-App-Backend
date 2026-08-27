from app.extensions import db
from app.models import MpesaTransaction, Transaction, User
from app.utils.errors import ApiError, ErrorCode
from app.utils.validators import normalize_kenyan_phone


class UserService:

    @staticmethod
    def get_user_by_id(user_id):
        return db.session.get(User, user_id)

    @staticmethod
    def find_by_public_identifier(phone_number=None, email=None):
        """Resolve a user-facing account identifier to a single user.

        ``email`` is matched case-insensitively and is guaranteed unique and
        present for every account. ``phone_number`` is matched against the exact
        submitted (canonical) value, with a fallback that normalises each stored
        value so differing formats (``07...`` vs ``254...`` vs ``+254...``) still
        match.

        Returns the :class:`User` or ``None`` when no account matches.
        """
        if email:
            return User.query.filter(User.email.ilike(email)).first()

        if phone_number:
            # Fast path: exact match on the canonical normalised value.
            exact = User.query.filter(User.phone_number == phone_number).first()
            if exact:
                return exact

            # Fallback: normalise each stored value (handles 07... vs 254... vs
            # +254... forms) and compare against the already-normalised input.
            for user in User.query.filter(User.phone_number.isnot(None)).all():
                try:
                    normalised_stored = normalize_kenyan_phone(user.phone_number)
                except ApiError:
                    continue
                if normalised_stored == phone_number:
                    return user

        return None

    @staticmethod
    def update_user(user, first_name=None, last_name=None, phone_number=None):
        if first_name is not None:
            user.first_name = first_name.strip()

        if last_name is not None:
            user.last_name = last_name.strip()

        if phone_number is not None:
            phone_number = phone_number.strip()

            existing_user = User.query.filter(User.phone_number == phone_number, User.id != user.id,).first()

            if existing_user:
                raise ValueError(
                    "A user with this phone number already exists"
                )
            user.phone_number = phone_number

        db.session.commit()

        return user

    @staticmethod
    def update_user_admin(user, first_name=None, last_name=None, phone_number=None, status=None):
        """Apply an admin-initiated update to permitted user fields.

        The status field controls account activation/freeze and is the safe
        alternative to deletion for accounts that carry financial history.
        """

        if first_name is not None:
            user.first_name = first_name.strip()

        if last_name is not None:
            user.last_name = last_name.strip()

        if phone_number is not None:
            phone_number = phone_number.strip()
            if phone_number:
                existing_user = User.query.filter(
                    User.phone_number == phone_number,
                    User.id != user.id,
                ).first()
                if existing_user:
                    raise ValueError(
                        "A user with this phone number already exists"
                    )
            user.phone_number = phone_number or None

        if status is not None:
            user.status = status

        db.session.commit()

        return user

    @staticmethod
    def has_financial_history(user_id):
        """Return True if the user has any transaction or M-Pesa financial record.

        These records form the audit trail and must never be destroyed.
        """

        has_transaction = (
            Transaction.query.filter(
                (Transaction.sender_id == user_id)
                | (Transaction.receiver_id == user_id)
            ).first()
            is not None
        )
        if has_transaction:
            return True

        has_mpesa = (
            MpesaTransaction.query.filter_by(user_id=user_id).first() is not None
        )
        return has_mpesa

    @staticmethod
    def delete_user(user):
        """Permanently delete a user with no financial history.

        Only call this after :meth:`has_financial_history` returned ``False``.
        The user's wallet and beneficiaries are removed through the existing
        cascade configuration; financial records are never present for such a
        user, so nothing in the audit trail is destroyed.
        """

        db.session.delete(user)
        db.session.commit()

        return True            