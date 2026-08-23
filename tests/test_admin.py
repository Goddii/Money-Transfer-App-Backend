"""Regression tests protecting admin/user separation.

These only assert that admin authorization is unchanged by the new user
endpoints; admin functionality itself is out of scope for this work.
"""

import pytest
from decimal import Decimal

from app.extensions import db
from app.models import Transaction, User, Wallet
from tests.conftest import DEFAULT_PASSWORD

OVERVIEW_URL = "/api/v1/admin/overview"
ADMIN_USERS_URL = "/api/v1/admin/users"
AUDIT_LOG_URL = "/api/v1/admin/audit-log"
REVENUE_URL = "/api/v1/admin/revenue-analytics"


def test_admin_overview_requires_authentication(client):
    assert client.get(OVERVIEW_URL).status_code == 401


def test_normal_user_cannot_access_admin_overview(client, authenticated_user):
    _, headers = authenticated_user(email="normal@example.com")

    response = client.get(OVERVIEW_URL, headers=headers)

    assert response.status_code == 403


def test_normal_user_cannot_list_admin_users(client, authenticated_user):
    _, headers = authenticated_user(email="normal@example.com")

    assert client.get(ADMIN_USERS_URL, headers=headers).status_code == 403


def test_admin_can_access_admin_overview(client, authenticated_user):
    _, headers = authenticated_user(email="admin@example.com", role="admin")

    response = client.get(OVERVIEW_URL, headers=headers)

    assert response.status_code == 200
    assert "total_users" in response.get_json()


def test_admin_endpoints_reject_normal_user_wallet_data_access(
    client, create_user, authenticated_user
):
    """A normal user must not reach admin user-management routes."""
    target = create_user(email="target@example.com", balance="100.00")
    _, headers = authenticated_user(email="normal@example.com")

    response = client.get(
        f"{ADMIN_USERS_URL}/{target['id']}/profile", headers=headers
    )

    assert response.status_code == 403


def test_admin_audit_log_presents_amounts_in_kes(client, app, authenticated_user):
    """Audit-log monetary values must be presented in KES, not '$'."""
    admin, headers = authenticated_user(email="admin@example.com", role="admin")

    with app.app_context():
        user = User(first_name="Aud", last_name="It", email="audituser@example.com")
        user.set_password(DEFAULT_PASSWORD)
        db.session.add(user)
        db.session.flush()
        db.session.add(Wallet(user_id=user.id, balance=Decimal("0.00")))
        db.session.add(
            Transaction(
                tx_code="TXKES1",
                sender_id=user.id,
                receiver_id=user.id,
                amount=Decimal("500.00"),
                fee=Decimal("25.00"),
                tx_type="Transfer",
            )
        )
        db.session.commit()

    response = client.get(AUDIT_LOG_URL, headers=headers)
    assert response.status_code == 200

    records = response.get_json()["audit_log"]
    assert records, "expected at least one audit record"
    for record in records:
        assert record["amount"].startswith("KES ")
        assert record["fee"].startswith("KES ")

    assert "$" not in response.get_data(as_text=True)


def test_admin_revenue_analytics_presents_amounts_in_kes(
    client, app, authenticated_user
):
    """Revenue-by-source monetary values must be presented in KES, not '$'.

    The route relies on PostgreSQL's ``to_char`` for monthly grouping, so this
    test is only meaningful against PostgreSQL; skip it on SQLite.
    """
    with app.app_context():
        if db.engine.dialect.name == "sqlite":
            pytest.skip("revenue-analytics uses PostgreSQL to_char")

    admin, headers = authenticated_user(email="admin2@example.com", role="admin")

    with app.app_context():
        user = User(first_name="Rev", last_name="Enue", email="revuser@example.com")
        user.set_password(DEFAULT_PASSWORD)
        db.session.add(user)
        db.session.flush()
        db.session.add(Wallet(user_id=user.id, balance=Decimal("0.00")))
        db.session.add(
            Transaction(
                tx_code="TXKES2",
                sender_id=user.id,
                receiver_id=user.id,
                amount=Decimal("100.00"),
                fee=Decimal("5.00"),
                tx_type="Transfer",
            )
        )
        db.session.commit()

    response = client.get(REVENUE_URL, headers=headers)
    assert response.status_code == 200

    data = response.get_json()
    assert data["revenue_by_source"], "expected at least one revenue source"
    for source in data["revenue_by_source"]:
        assert source["amount"].startswith("KES ")

    assert "$" not in response.get_data(as_text=True)
