"""Tests for the admin platform analytics endpoint."""

from decimal import Decimal

from app.extensions import db
from app.models import Transaction, User, Wallet
from app.models.transaction import TransactionStatus, TransactionType

PLATFORM_URL = "/api/v1/admin/platform"


def test_platform_analytics_requires_admin(client, authenticated_user):
    _, headers = authenticated_user(email="normalp@example.com")

    response = client.get(PLATFORM_URL, headers=headers)
    assert response.status_code == 403


def test_platform_analytics_returns_expected_keys(
    client, app, authenticated_user, create_user
):
    admin, headers = authenticated_user(email="adminp@example.com", role="admin")
    create_user(email="pu1@example.com", balance="100.00")
    create_user(email="pu2@example.com", balance="50.00")

    with app.app_context():
        user = User(first_name="Tx", last_name="User", email="ptx@example.com")
        user.set_password("SecurePass123")
        db.session.add(user)
        db.session.flush()
        db.session.add(Wallet(user_id=user.id, balance=Decimal("0.00")))
        db.session.add(
            Transaction(
                tx_code="TXPLT1",
                sender_id=user.id,
                receiver_id=user.id,
                amount=Decimal("250.00"),
                fee=Decimal("5.00"),
                tx_type=TransactionType.TRANSFER,
            )
        )
        db.session.commit()

    response = client.get(PLATFORM_URL, headers=headers)

    assert response.status_code == 200, response.get_json()
    data = response.get_json()

    expected_keys = {
        "volumeMonthly",
        "volumeDelta",
        "newUsersMo",
        "newUsersNote",
        "avgTxSize",
        "avgTxSizeNote",
        "growthCurve",
        "mostActive",
    }
    assert expected_keys.issubset(set(data.keys()))

    assert len(data["growthCurve"]) == 12
    assert data["growthCurve"][0]["month"]
    # at least one user should appear in most-active given the seeded transfer
    assert any(entry["name"] for entry in data["mostActive"])
