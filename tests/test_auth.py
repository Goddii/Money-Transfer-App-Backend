"""Authentication tests: registration, login and JWT enforcement."""

from app.extensions import db
from app.models import User, Wallet
from app.services.auth_service import AuthService
from tests.conftest import DEFAULT_PASSWORD

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"

VALID_REGISTRATION = {
    "first_name": "Grace",
    "last_name": "Wanjiku",
    "email": "grace@example.com",
    "password": DEFAULT_PASSWORD,
    "phone_number": "0712345678",
}


def test_register_success(client, app):
    response = client.post(REGISTER_URL, json=VALID_REGISTRATION)

    assert response.status_code == 201

    payload = response.get_json()

    assert payload["success"] is True
    assert payload["data"]["user"]["email"] == "grace@example.com"
    assert payload["data"]["user"]["role"] == "user"
    # Sensitive fields must never be returned.
    assert "password" not in payload["data"]["user"]
    assert "password_hash" not in payload["data"]["user"]

    with app.app_context():
        user = User.query.filter_by(email="grace@example.com").first()

        assert user is not None
        assert user.password_hash != DEFAULT_PASSWORD


def test_register_duplicate_email_rejected(client):
    client.post(REGISTER_URL, json=VALID_REGISTRATION)

    response = client.post(REGISTER_URL, json=VALID_REGISTRATION)

    assert response.status_code == 409
    assert response.get_json()["success"] is False
    assert response.get_json()["error"] == "DUPLICATE_RESOURCE"


def test_register_duplicate_phone_number_rejected(client):
    client.post(REGISTER_URL, json=VALID_REGISTRATION)

    duplicate_phone = dict(VALID_REGISTRATION, email="other@example.com")

    response = client.post(REGISTER_URL, json=duplicate_phone)

    assert response.status_code == 409
    assert response.get_json()["error"] == "DUPLICATE_RESOURCE"


def test_register_missing_fields_rejected(client):
    response = client.post(REGISTER_URL, json={"email": "a@example.com"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "VALIDATION_ERROR"


def test_register_short_password_rejected(client):
    response = client.post(
        REGISTER_URL, json=dict(VALID_REGISTRATION, password="short")
    )

    assert response.status_code == 400


def test_register_rolls_back_when_wallet_creation_fails(client, app, monkeypatch):
    """A failed wallet creation must not leave an orphaned user behind."""

    def _explode(user_id):
        raise RuntimeError("wallet failure")

    monkeypatch.setattr(
        "app.services.auth_service.WalletService.create_wallet", _explode
    )

    response = client.post(REGISTER_URL, json=VALID_REGISTRATION)

    assert response.status_code == 500

    with app.app_context():
        assert User.query.filter_by(email="grace@example.com").first() is None
        assert Wallet.query.count() == 0


def test_login_success_returns_token_and_user(client):
    client.post(REGISTER_URL, json=VALID_REGISTRATION)

    response = client.post(
        LOGIN_URL,
        json={"email": "grace@example.com", "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 200

    data = response.get_json()["data"]

    # The existing frontend depends on these exact fields.
    assert data["access_token"]
    assert data["user"]["email"] == "grace@example.com"
    assert "password_hash" not in data["user"]


def test_login_invalid_password_rejected(client):
    client.post(REGISTER_URL, json=VALID_REGISTRATION)

    response = client.post(
        LOGIN_URL,
        json={"email": "grace@example.com", "password": "WrongPassword1"},
    )

    assert response.status_code == 401
    assert response.get_json()["success"] is False


def test_login_unknown_email_rejected(client):
    response = client.post(
        LOGIN_URL, json={"email": "nobody@example.com", "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 401


def test_login_missing_credentials_rejected(client):
    response = client.post(LOGIN_URL, json={})

    assert response.status_code == 401


def test_login_inactive_user_rejected(client, create_user):
    user = create_user(email="inactive@example.com", is_active=False)

    response = client.post(
        LOGIN_URL, json={"email": user["email"], "password": user["password"]}
    )

    assert response.status_code == 401


def test_login_frozen_user_rejected(client, create_user):
    user = create_user(email="frozen@example.com", status="Frozen")

    response = client.post(
        LOGIN_URL, json={"email": user["email"], "password": user["password"]}
    )

    assert response.status_code == 401


def test_login_unexpected_error_does_not_leak_internal_details(
    client, create_user, monkeypatch
):
    user = create_user(email="leak@example.com")

    secret_detail = "psycopg2 connection string postgres://secret"

    def _explode(email, password):
        raise RuntimeError(secret_detail)

    monkeypatch.setattr(AuthService, "login_user", _explode)

    response = client.post(
        LOGIN_URL, json={"email": user["email"], "password": user["password"]}
    )

    assert response.status_code == 500

    body = response.get_data(as_text=True)

    assert secret_detail not in body
    assert "RuntimeError" not in body
    assert response.get_json()["message"] == "An unexpected error occurred"


def test_protected_route_requires_token(client):
    response = client.get("/api/users/me")

    assert response.status_code == 401
    assert response.get_json()["error"] == "AUTH_REQUIRED"


def test_protected_route_rejects_invalid_token(client):
    response = client.get(
        "/api/users/me", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401


def test_token_of_deleted_user_is_rejected(client, app, authenticated_user):
    user, headers = authenticated_user(email="ghost@example.com")

    with app.app_context():
        db.session.delete(db.session.get(User, user["id"]))
        db.session.commit()

    response = client.get("/api/users/me", headers=headers)

    assert response.status_code == 401
    assert response.get_json()["error"] == "USER_NOT_FOUND"


def test_token_of_deactivated_user_is_rejected(client, app, authenticated_user):
    user, headers = authenticated_user(email="deactivated@example.com")

    with app.app_context():
        db_user = db.session.get(User, user["id"])
        db_user.is_active = False
        db.session.commit()

    response = client.get("/api/users/me", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "ACCOUNT_INACTIVE"


def test_token_of_frozen_user_is_rejected(client, app, authenticated_user):
    user, headers = authenticated_user(email="tofreeze@example.com")

    with app.app_context():
        db_user = db.session.get(User, user["id"])
        db_user.status = "Frozen"
        db.session.commit()

    response = client.get("/api/wallet", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "ACCOUNT_INACTIVE"
