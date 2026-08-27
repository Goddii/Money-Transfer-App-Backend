"""Tests for admin user CRUD: create, update, and safe delete."""

from decimal import Decimal

from app.extensions import db
from app.models import Transaction, User, Wallet
from app.models.transaction import TransactionStatus, TransactionType

ADMIN_USERS_URL = "/api/v1/admin/users"


def _give_financial_history(app, user_id, counterparty_id):
    with app.app_context():
        tx = Transaction(
            tx_code="TXHIST1",
            sender_id=user_id,
            receiver_id=counterparty_id,
            amount=Decimal("10.00"),
            fee=Decimal("0.00"),
            status=TransactionStatus.COMPLETED,
            tx_type=TransactionType.TRANSFER,
        )
        db.session.add(tx)
        db.session.commit()


def test_admin_create_user_success(client, app, authenticated_user):
    admin, headers = authenticated_user(email="adminc@example.com", role="admin")

    response = client.post(
        ADMIN_USERS_URL,
        headers=headers,
        json={
            "name": "New User",
            "email": "created@example.com",
            "phone": "+254700000000",
            "password": "Password123",
            "initial_balance": 500,
        },
    )

    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["message"] == "User created successfully"
    assert "user_id" in body

    new_id = body["user_id"]
    with app.app_context():
        user = db.session.get(User, new_id)
        assert user is not None
        assert user.email == "created@example.com"
        assert user.name == "New User"
        wallet = Wallet.query.filter_by(user_id=new_id).first()
        assert wallet is not None
        assert wallet.balance == Decimal("500.00")


def test_admin_create_user_duplicate_email(client, app, authenticated_user):
    admin, headers = authenticated_user(email="admindup@example.com", role="admin")
    target = User(
        first_name="Dup", last_name="Lic", email="dup@example.com"
    )
    target.set_password("SecurePass123")
    with app.app_context():
        db.session.add(target)
        db.session.commit()

    response = client.post(
        ADMIN_USERS_URL,
        headers=headers,
        json={
            "name": "Dup Lic",
            "email": "dup@example.com",
            "password": "Password123",
        },
    )

    assert response.status_code == 409


def test_admin_create_user_validation_error(client, app, authenticated_user):
    admin, headers = authenticated_user(email="adminval@example.com", role="admin")

    response = client.post(
        ADMIN_USERS_URL, headers=headers, json={"name": "No Email"}
    )

    assert response.status_code == 400


def test_admin_update_user_fields(client, app, authenticated_user):
    admin, headers = authenticated_user(email="adminu@example.com", role="admin")
    target = User(
        first_name="Old", last_name="Name", email="upd@example.com"
    )
    target.set_password("SecurePass123")
    with app.app_context():
        db.session.add(target)
        db.session.commit()
        target_id = target.id

    response = client.patch(
        f"{ADMIN_USERS_URL}/{target_id}",
        headers=headers,
        json={
            "first_name": "New",
            "last_name": "Name",
            "status": "Frozen",
        },
    )

    assert response.status_code == 200, response.get_json()
    with app.app_context():
        updated = db.session.get(User, target_id)
        assert updated.first_name == "New"
        assert updated.status == "Frozen"


def test_admin_update_user_rejects_invalid_status(client, app, authenticated_user):
    admin, headers = authenticated_user(email="adminus@example.com", role="admin")
    target = User(
        first_name="Bad", last_name="Status", email="badstatus@example.com"
    )
    target.set_password("SecurePass123")
    with app.app_context():
        db.session.add(target)
        db.session.commit()
        target_id = target.id

    response = client.patch(
        f"{ADMIN_USERS_URL}/{target_id}",
        headers=headers,
        json={"status": "Banned"},
    )

    assert response.status_code == 400


def test_admin_update_user_rejects_sensitive_fields(
    client, app, authenticated_user
):
    admin, headers = authenticated_user(email="adminus2@example.com", role="admin")
    target = User(
        first_name="Sens", last_name="Itive", email="sens@example.com"
    )
    target.set_password("SecurePass123")
    with app.app_context():
        db.session.add(target)
        db.session.commit()
        target_id = target.id

    response = client.patch(
        f"{ADMIN_USERS_URL}/{target_id}",
        headers=headers,
        json={"role": "admin"},
    )

    assert response.status_code == 400


def test_normal_user_cannot_update_user(client, app, authenticated_user, create_user):
    _, headers = authenticated_user(email="normalu@example.com")
    target = create_user(email="targetu@example.com")

    response = client.patch(
        f"{ADMIN_USERS_URL}/{target['id']}",
        headers=headers,
        json={"first_name": "Hacked"},
    )

    assert response.status_code == 403


def test_admin_delete_empty_user_succeeds(client, app, authenticated_user):
    admin, headers = authenticated_user(email="admind@example.com", role="admin")
    target = User(
        first_name="Empty", last_name="Account", email="emptyu@example.com"
    )
    target.set_password("SecurePass123")
    with app.app_context():
        db.session.add(target)
        db.session.commit()
        target_id = target.id
        assert db.session.get(User, target_id) is not None

    response = client.delete(
        f"{ADMIN_USERS_URL}/{target_id}", headers=headers
    )

    assert response.status_code == 200, response.get_json()
    with app.app_context():
        assert db.session.get(User, target_id) is None


def test_admin_delete_user_with_history_returns_409(client, app, authenticated_user, create_user):
    admin, headers = authenticated_user(email="admindh@example.com", role="admin")
    target = create_user(email="historyu@example.com", balance="100.00")
    counterparty = create_user(email="counteru@example.com", balance="100.00")

    _give_financial_history(app, target["id"], counterparty["id"])

    response = client.delete(
        f"{ADMIN_USERS_URL}/{target['id']}", headers=headers
    )

    assert response.status_code == 409, response.get_json()
    assert "financial history" in response.get_json()["message"]

    with app.app_context():
        assert db.session.get(User, target["id"]) is not None


def test_normal_user_cannot_delete_user(client, app, authenticated_user, create_user):
    _, headers = authenticated_user(email="normald@example.com")
    target = create_user(email="targetd@example.com")

    response = client.delete(
        f"{ADMIN_USERS_URL}/{target['id']}", headers=headers
    )

    assert response.status_code == 403
