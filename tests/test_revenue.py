"""Tests for the refactored (database-agnostic) revenue analytics endpoint."""

from decimal import Decimal

from app.extensions import db
from app.models import Transaction, User, Wallet
from app.models.transaction import TransactionStatus, TransactionType

REVENUE_URL = "/api/v1/admin/revenue-analytics"


def _seed_fees(app):
    with app.app_context():
        user = User(first_name="Rev", last_name="Enue", email="revseed@example.com")
        user.set_password("SecurePass123")
        db.session.add(user)
        db.session.flush()
        db.session.add(Wallet(user_id=user.id, balance=Decimal("0.00")))
        # Two transfer fees of 5.00 and one deposit fee of 2.50.
        db.session.add(
            Transaction(
                tx_code="TXREV1",
                sender_id=user.id,
                receiver_id=user.id,
                amount=Decimal("100.00"),
                fee=Decimal("5.00"),
                tx_type=TransactionType.TRANSFER,
            )
        )
        db.session.add(
            Transaction(
                tx_code="TXREV2",
                sender_id=user.id,
                receiver_id=user.id,
                amount=Decimal("200.00"),
                fee=Decimal("5.00"),
                tx_type=TransactionType.TRANSFER,
            )
        )
        db.session.add(
            Transaction(
                tx_code="TXREV3",
                sender_id=None,
                receiver_id=user.id,
                amount=Decimal("300.00"),
                fee=Decimal("2.50"),
                tx_type=TransactionType.DEPOSIT,
            )
        )
        db.session.commit()


def test_revenue_analytics_runs_on_sqlite(client, app, authenticated_user):
    """This previously skipped on SQLite; now it must pass without dialect tricks."""
    admin, headers = authenticated_user(email="adminr@example.com", role="admin")
    _seed_fees(app)

    response = client.get(REVENUE_URL, headers=headers)

    assert response.status_code == 200, response.get_json()
    data = response.get_json()

    # Naive parse of "KES 12.50" style strings to confirm the sum.
    by_source_total = sum(
        Decimal(source["amount"].replace("KES ", ""))
        for source in data["revenue_by_source"]
    )
    assert by_source_total == Decimal("12.50")

    assert data["revenue_trend_months"]
    # Every monthly entry must carry a month label and a numeric revenue.
    for entry in data["revenue_trend_months"]:
        assert entry["month"]
        assert isinstance(entry["revenue"], (int, float))
