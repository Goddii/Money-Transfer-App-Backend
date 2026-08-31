"""Shared pytest fixtures for the Vyloc backend test suite.

Tests run against an isolated temporary SQLite database by default. Set
``TEST_DATABASE_URL`` to run the identical suite against PostgreSQL, for
example::

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/vyloc_test pytest

No test performs a real Safaricom Daraja request.
"""

import os
from decimal import Decimal

import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User, Wallet
from app.services.mpesa_service import MpesaService


@pytest.fixture(autouse=True)
def _isolate_daraja_state(app):
    """Reset Daraja token cache + rate-limiter/cooldown between tests.

    Guarantees environment/credential isolation for the token cache and stops a
    global upstream cooldown or throttled budget set by one test from leaking
    into the next, so the focused Daraja tests do not depend on execution order.
    """
    with app.app_context():
        MpesaService.reset_token_cache()
        MpesaService.reset_daraja_throttle()
    yield

DEFAULT_PASSWORD = "SecurePass123"


@pytest.fixture
def app(tmp_path):
    database_uri = os.environ.get("TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path / 'vyloc-test.db'}"
    )

    class _TestConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = database_uri

    flask_app = create_app(_TestConfig)

    with flask_app.app_context():
        db.drop_all()
        db.create_all()

    yield flask_app

    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def create_user(app):
    """Create a user (with wallet) directly in the database.

    Used when a test needs a specific balance or account status. Registration
    itself is covered by the auth tests.
    """

    def _create_user(
        email="user@example.com",
        password=DEFAULT_PASSWORD,
        first_name="Test",
        last_name="User",
        phone_number=None,
        role="user",
        is_active=True,
        status="Active",
        balance="0.00",
        with_wallet=True,
    ):
        with app.app_context():
            user = User(
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone_number=phone_number,
                role=role,
                is_active=is_active,
                status=status,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()

            if with_wallet:
                db.session.add(
                    Wallet(user_id=user.id, balance=Decimal(balance))
                )

            db.session.commit()

            return {
                "id": user.id,
                "email": email,
                "password": password,
                "name": user.name,
            }

    return _create_user


@pytest.fixture
def login(client):
    """Log in through the API and return an Authorization header."""

    def _login(email, password=DEFAULT_PASSWORD):
        response = client.post(
            "/api/auth/login", json={"email": email, "password": password}
        )

        assert response.status_code == 200, response.get_json()

        token = response.get_json()["data"]["access_token"]

        return {"Authorization": f"Bearer {token}"}

    return _login


@pytest.fixture
def authenticated_user(create_user, login):
    """Create an active user with a balance and return the user plus headers."""

    def _authenticated_user(email="user@example.com", balance="0.00", **kwargs):
        user = create_user(email=email, balance=balance, **kwargs)
        headers = login(email, user["password"])

        return user, headers

    return _authenticated_user


@pytest.fixture
def wallet_balance(app):
    """Read a user's wallet balance as a Decimal."""

    def _wallet_balance(user_id):
        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user_id).first()

            return Decimal(str(wallet.balance)) if wallet else None

    return _wallet_balance
