"""Idempotent first-start seeding for the Vyloc backend.

Run as part of the deployment start command, after migrations have been
applied::

    flask --app run.py db upgrade && python seed.py && gunicorn run:app

Creates the single administrator account the admin panel needs in order to be
usable on a brand-new database. Running it repeatedly is safe: the admin is
looked up by email first, so a redeploy or restart never creates a duplicate.

Credentials are supplied entirely through the environment:

``ADMIN_EMAIL``
    Optional. Defaults to ``admin@example.com`` for local and demo use.

``ADMIN_PASSWORD``
    Required, with no fallback. A hardcoded default password would ship a
    publicly known administrator credential to every deployment, so seeding
    fails loudly instead. Because the deployment start command is
    ``&&``-chained, that failure stops the release rather than exposing a
    guessable admin account.

The password is only ever used to compute a hash. It is never printed,
logged, echoed into an exception message, or persisted in plaintext.
"""

import os
import sys

from app import create_app
from app.extensions import db
from app.models.user import User

# Safe for local/demo development; override with ADMIN_EMAIL in a deployment.
DEFAULT_ADMIN_EMAIL = 'admin@example.com'

ADMIN_PASSWORD_ENV = 'ADMIN_PASSWORD'
ADMIN_EMAIL_ENV = 'ADMIN_EMAIL'

MISSING_PASSWORD_MESSAGE = (
    f"{ADMIN_PASSWORD_ENV} is not set. Refusing to seed the administrator "
    "account with a default password. Set a strong "
    f"{ADMIN_PASSWORD_ENV} environment variable (and optionally "
    f"{ADMIN_EMAIL_ENV}) on the service, then run the deployment again."
)


class SeedConfigError(RuntimeError):
    """Required seed configuration is missing or unusable.

    The message intentionally names only the environment variable, never any
    supplied value, so the error is safe to surface in deployment logs.
    """


def get_admin_email():
    """Return the administrator email, falling back to the demo default."""
    email = (os.environ.get(ADMIN_EMAIL_ENV) or '').strip()

    return (email or DEFAULT_ADMIN_EMAIL).lower()


def get_admin_password():
    """Return the administrator password from the environment.

    Raises ``SeedConfigError`` when it is missing or blank. There is
    deliberately no default: see the module docstring.
    """
    password = os.environ.get(ADMIN_PASSWORD_ENV) or ''

    if not password.strip():
        raise SeedConfigError(MISSING_PASSWORD_MESSAGE)

    return password


def seed_admin():
    """Create the administrator account if it does not already exist.

    Must be called inside a Flask application context. Returns
    ``(user, created)`` where ``created`` is ``False`` when an admin with the
    configured email was already present.
    """
    email = get_admin_email()

    existing = User.query.filter_by(email=email).first()
    if existing:
        return existing, False

    # Read the password only after confirming a new account is needed, but
    # before writing anything, so a missing password can never leave a
    # half-seeded row behind.
    password = get_admin_password()

    admin = User(
        first_name='Admin',
        last_name='User',
        email=email,
        role='admin',
        is_active=True,
        status='Active',
    )
    admin.set_password(password)

    db.session.add(admin)
    db.session.commit()

    return admin, True


def main():
    """Entry point for ``python seed.py``. Returns a process exit code."""
    app = create_app()

    with app.app_context():
        try:
            admin, created = seed_admin()
        except SeedConfigError as error:
            # Non-zero exit stops the && chain so the service does not start
            # without a properly configured administrator.
            print(f'Seeding failed: {error}', file=sys.stderr)
            return 1

        if created:
            # Never print the password.
            print(
                f'Admin created with id={admin.id}, email={admin.email}, '
                f'role={admin.role}'
            )
        else:
            print(f'Admin already exists with id={admin.id}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
