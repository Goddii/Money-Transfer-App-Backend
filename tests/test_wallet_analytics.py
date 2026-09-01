"""Tests for the user wallet analytics endpoint."""

from decimal import Decimal

from app.extensions import db
from app.models import Transaction, User, Wallet
from app.models.transaction import TransactionStatus, TransactionType


WALLET_ANALYTICS_URL = "/api/wallet/analytics"


def _make_transaction(app, **kwargs):
    with app.app_context():
        tx = Transaction(
            tx_code=kwargs["tx_code"],
            sender_id=kwargs.get("sender_id"),
            receiver_id=kwargs["receiver_id"],
            amount=Decimal(str(kwargs["amount"])),
            fee=Decimal("0.00"),
            status=kwargs.get("status", TransactionStatus.COMPLETED),
            tx_type=kwargs["tx_type"],
        )
        db.session.add(tx)
        db.session.commit()


def test_wallet_analytics_requires_authentication(client):
    assert client.get(WALLET_ANALYTICS_URL).status_code == 401


def test_wallet_analytics_returns_correct_aggregates(
    client, app, authenticated_user
):
    user, headers = authenticated_user(email="analytics@example.com", balance="1500.00")
    other = User(first_name="Oth", last_name="Er", email="other@example.com")
    other.set_password("SecurePass123")
    with app.app_context():
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    _make_transaction(
        app, tx_code="TXANA1", receiver_id=user["id"], amount="1000.00",
        tx_type=TransactionType.DEPOSIT,
    )
    _make_transaction(
        app, tx_code="TXANA2", sender_id=user["id"], receiver_id=other_id,
        amount="200.00", tx_type=TransactionType.TRANSFER,
    )
    _make_transaction(
        app, tx_code="TXANA3", sender_id=other_id, receiver_id=user["id"],
        amount="300.00", tx_type=TransactionType.TRANSFER,
    )

    response = client.get(WALLET_ANALYTICS_URL, headers=headers)

    assert response.status_code == 200
    data = response.get_json()["data"]["analytics"]

    assert data["current_balance"] == 1500.00
    assert data["total_deposits"] == 1000.00
    assert data["total_sent"] == 200.00
    assert data["total_received"] == 300.00
    assert data["total_transfers"] == 2
    assert data["transaction_count"] == 3
    assert len(data["monthly_trend"]) == 6


def test_wallet_analytics_empty_user(client, app, authenticated_user):
    user, headers = authenticated_user(email="empty@example.com", balance="0.00")

    response = client.get(WALLET_ANALYTICS_URL, headers=headers)

    assert response.status_code == 200
    data = response.get_json()["data"]["analytics"]

    assert data["current_balance"] == 0.0
    assert data["total_deposits"] == 0.0
    assert data["total_sent"] == 0.0
    assert data["total_received"] == 0.0
    assert data["total_transfers"] == 0
    assert data["transaction_count"] == 0
    assert len(data["monthly_trend"]) == 6
