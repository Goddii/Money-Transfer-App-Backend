"""Tests for wallet creation and the wallet endpoint."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import LedgerEntryType, User, Wallet, WalletLedger
from tests.conftest import DEFAULT_PASSWORD

WALLET_URL = "/api/wallet"


def test_wallet_is_created_during_registration(client, app):
    response = client.post(
        "/api/auth/register",
        json={
            "first_name": "Wallet",
            "last_name": "Owner",
            "email": "walletowner@example.com",
            "password": DEFAULT_PASSWORD,
        },
    )

    assert response.status_code == 201

    user_id = response.get_json()["data"]["user"]["id"]

    with app.app_context():
        wallets = Wallet.query.filter_by(user_id=user_id).all()

        assert len(wallets) == 1
        assert Decimal(str(wallets[0].balance)) == Decimal("0.00")


def test_registration_does_not_create_duplicate_wallets(client, app):
    payload = {
        "first_name": "Single",
        "last_name": "Wallet",
        "email": "single@example.com",
        "password": DEFAULT_PASSWORD,
    }

    client.post("/api/auth/register", json=payload)
    # A second attempt with the same email must be rejected without creating
    # another wallet.
    client.post("/api/auth/register", json=payload)

    with app.app_context():
        user = User.query.filter_by(email="single@example.com").first()

        assert Wallet.query.filter_by(user_id=user.id).count() == 1


def test_get_wallet_requires_authentication(client):
    response = client.get(WALLET_URL)

    assert response.status_code == 401
    assert response.get_json()["error"] == "AUTH_REQUIRED"


def test_authenticated_user_retrieves_own_wallet(client, authenticated_user):
    user, headers = authenticated_user(email="owner@example.com", balance="150.75")

    response = client.get(WALLET_URL, headers=headers)

    assert response.status_code == 200

    payload = response.get_json()

    assert payload["success"] is True

    wallet = payload["data"]["wallet"]

    assert wallet["user_id"] == user["id"]
    # Money is returned as an exact 2dp string, never a float.
    assert wallet["balance"] == "150.75"
    assert isinstance(wallet["balance"], str)
    assert set(wallet.keys()) == {
        "id",
        "user_id",
        "balance",
        "currency",
        "created_at",
    }


def test_wallet_endpoint_never_returns_another_users_wallet(
    client, create_user, authenticated_user
):
    other = create_user(email="other@example.com", balance="9999.00")
    user, headers = authenticated_user(email="me@example.com", balance="10.00")

    response = client.get(WALLET_URL, headers=headers)

    wallet = response.get_json()["data"]["wallet"]

    assert wallet["user_id"] == user["id"]
    assert wallet["user_id"] != other["id"]
    assert wallet["balance"] == "10.00"


def test_missing_wallet_returns_not_found(client, app, authenticated_user):
    user, headers = authenticated_user(email="nowallet@example.com")

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        db.session.delete(wallet)
        db.session.commit()

    response = client.get(WALLET_URL, headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "WALLET_NOT_FOUND"


def test_database_rejects_a_second_wallet_for_the_same_user(app, create_user):
    """The one-wallet-per-user rule is enforced by the database."""
    user = create_user(email="onewallet@example.com")

    with app.app_context():
        db.session.add(Wallet(user_id=user["id"], balance=Decimal("0.00")))

        with pytest.raises(IntegrityError):
            db.session.commit()

        db.session.rollback()

        assert Wallet.query.filter_by(user_id=user["id"]).count() == 1


def test_database_rejects_duplicate_ledger_reference(app, create_user):
    """The ledger cannot record the same payment reference twice for a wallet."""
    user = create_user(email="ledger@example.com")

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()

        for _ in range(2):
            db.session.add(
                WalletLedger(
                    wallet_id=wallet.id,
                    entry_type=LedgerEntryType.CREDIT,
                    amount=Decimal("100.00"),
                    balance_before=Decimal("0.00"),
                    balance_after=Decimal("100.00"),
                    reference="QK12AB34CD",
                )
            )

        with pytest.raises(IntegrityError):
            db.session.commit()

        db.session.rollback()
