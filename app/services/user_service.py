from app.extensions import db
from app.models.user import User


class UserService:

    @staticmethod
    def get_user_by_id(user_id):
        return db.session.get(User, user_id)

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