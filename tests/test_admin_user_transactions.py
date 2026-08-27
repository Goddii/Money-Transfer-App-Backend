"""Tests for the admin per-user transaction history endpoint."""

from decimal import Decimal

from app.extensions import db
from app.models import Transaction, User, Wallet
from app.models.transaction import TransactionStatus, TransactionType

USER_TX_URL = "/api/v1/admin/users/{user_id}/transactions"


def _seed_transactions(app, user_id, counterparty_id):
    with app.app_context():
        for i in range(3):
            db.session.add(
                Transaction(
                    tx_code=f"TXUSR{i}",
                    sender_id=user_id if i % 2 == 0 else counterparty_id,
                    receiver_id=counterparty_id if i % 2 == 0 else user_id,
                    amount=Decimal(f"{10 + i}.00"),
                    fee=Decimal("0.00"),
                    tx_type=TransactionType.TRANSFER,
                )
            )
        # a transaction belonging to someone else only
        other = User(first_name="Str", last_name="Ager", email="stranger@example.com")
        other.set_password("SecurePass123")
        db.session.add(other)
        db.session.flush()
        db.session.add(Wallet(user_id=other.id, balance=Decimal("0.00")))
        db.session.add(
            Transaction(
                tx_code="TXSTR1",
                sender_id=other.id,
                receiver_id=other.id,
                amount=Decimal("999.00"),
                fee=Decimal("0.00"),
                tx_type=TransactionType.TRANSFER,
            )
        )
        db.session.commit()


def test_admin_user_transactions_requires_admin(client, authenticated_user):
    _, headers = authenticated_user(email="normalt@example.com")

    response = client.get(USER_TX_URL.format(user_id=1), headers=headers)
    assert response.status_code == 403


def test_admin_user_transactions_returns_only_that_user(
    client, app, authenticated_user, create_user
):
    admin, headers = authenticated_user(email="admint@example.com", role="admin")
    target = create_user(email="targett@example.com", balance="100.00")
    counterparty = create_user(email="countert@example.com", balance="100.00")

    _seed_transactions(app, target["id"], counterparty["id"])

    response = client.get(
        USER_TX_URL.format(user_id=target["id"]), headers=headers
    )

    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert data["user_id"] == target["id"]
    assert len(data["transactions"]) == 3
    for tx in data["transactions"]:
        assert tx["tx_code"].startswith("TXUSR")
        # never expose the stranger's transaction
        assert tx["tx_code"] != "TXSTR1"
    assert data["pagination"]["total"] == 3


def test_admin_user_transactions_pagination(
    client, app, authenticated_user, create_user
):
    admin, headers = authenticated_user(email="adminpg@example.com", role="admin")
    target = create_user(email="targetpg@example.com", balance="100.00")
    counterparty = create_user(email="counterpg@example.com", balance="100.00")

    _seed_transactions(app, target["id"], counterparty["id"])

    response = client.get(
        USER_TX_URL.format(user_id=target["id"]) + "?per_page=1&page=1",
        headers=headers,
    )

    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert len(data["transactions"]) == 1
    assert data["pagination"]["total"] == 3
    assert data["pagination"]["pages"] == 3
