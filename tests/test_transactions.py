"""Tests for peer-to-peer transfers, transaction history and detail."""

from decimal import Decimal

from app.extensions import db
from app.models import (
    LedgerEntryType,
    Transaction,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletLedger,
)

TRANSFER_URL = "/api/transactions/transfer"
TRANSACTIONS_URL = "/api/transactions"


def _balance(app, user_id):
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()

        return Decimal(str(wallet.balance))


# --- transfer ----------------------------------------------------------


def test_transfer_requires_authentication(client, create_user):
    receiver = create_user(email="receiver@example.com")

    response = client.post(
        TRANSFER_URL, json={"receiver_id": receiver["id"], "amount": "10.00"}
    )

    assert response.status_code == 401


def test_successful_transfer_updates_both_wallets_and_records_transaction(
    client, app, create_user, authenticated_user
):
    receiver = create_user(email="receiver@example.com", balance="5.00")
    sender, headers = authenticated_user(email="sender@example.com", balance="100.00")

    response = client.post(
        TRANSFER_URL,
        headers=headers,
        json={"receiver_id": receiver["id"], "amount": "25.50", "note": "Lunch"},
    )

    assert response.status_code == 201

    payload = response.get_json()

    assert payload["success"] is True

    transaction = payload["data"]["transaction"]

    assert transaction["amount"] == "25.50"
    assert transaction["fee"] == "0.00"
    assert transaction["status"] == TransactionStatus.COMPLETED
    assert transaction["tx_type"] == TransactionType.TRANSFER
    assert transaction["note"] == "Lunch"
    assert transaction["tx_code"]
    assert transaction["direction"] == "out"
    assert transaction["sender"]["id"] == sender["id"]
    assert transaction["receiver"]["id"] == receiver["id"]
    assert payload["data"]["wallet"]["balance"] == "74.50"

    # Exact decimal balances.
    assert _balance(app, sender["id"]) == Decimal("74.50")
    assert _balance(app, receiver["id"]) == Decimal("30.50")

    with app.app_context():
        stored = Transaction.query.one()

        assert stored.sender_id == sender["id"]
        assert stored.receiver_id == receiver["id"]
        assert Decimal(str(stored.amount)) == Decimal("25.50")
        assert Decimal(str(stored.fee)) == Decimal("0.00")

        entries = WalletLedger.query.order_by(WalletLedger.id).all()

        assert len(entries) == 2

        debit = next(e for e in entries if e.entry_type == LedgerEntryType.DEBIT)
        credit = next(e for e in entries if e.entry_type == LedgerEntryType.CREDIT)

        assert debit.reference == stored.tx_code
        assert Decimal(str(debit.balance_before)) == Decimal("100.00")
        assert Decimal(str(debit.balance_after)) == Decimal("74.50")
        assert Decimal(str(credit.balance_before)) == Decimal("5.00")
        assert Decimal(str(credit.balance_after)) == Decimal("30.50")
        assert debit.transaction_id == stored.id
        assert credit.transaction_id == stored.id


def test_transfer_rejected_when_balance_is_insufficient(
    client, app, create_user, authenticated_user
):
    receiver = create_user(email="receiver@example.com", balance="0.00")
    sender, headers = authenticated_user(email="sender@example.com", balance="10.00")

    response = client.post(
        TRANSFER_URL,
        headers=headers,
        json={"receiver_id": receiver["id"], "amount": "10.01"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "INSUFFICIENT_BALANCE"

    assert _balance(app, sender["id"]) == Decimal("10.00")
    assert _balance(app, receiver["id"]) == Decimal("0.00")

    with app.app_context():
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


def test_transfer_rejects_invalid_amounts(client, app, create_user, authenticated_user):
    receiver = create_user(email="receiver@example.com")
    sender, headers = authenticated_user(email="sender@example.com", balance="100.00")

    invalid_amounts = ["0", "0.00", "-5", "abc", "", None, "1.005", "1e400", True]

    for amount in invalid_amounts:
        response = client.post(
            TRANSFER_URL,
            headers=headers,
            json={"receiver_id": receiver["id"], "amount": amount},
        )

        assert response.status_code == 400, amount
        assert response.get_json()["error"] == "INVALID_AMOUNT", amount

    assert _balance(app, sender["id"]) == Decimal("100.00")

    with app.app_context():
        assert Transaction.query.count() == 0


def test_transfer_requires_receiver_and_amount(client, authenticated_user):
    _, headers = authenticated_user(email="sender@example.com", balance="100.00")

    assert client.post(TRANSFER_URL, headers=headers, json={}).status_code == 400
    assert (
        client.post(TRANSFER_URL, headers=headers, json={"amount": "5.00"}).status_code
        == 400
    )
    assert (
        client.post(
            TRANSFER_URL, headers=headers, json={"receiver_id": 1}
        ).status_code
        == 400
    )


def test_transfer_ignores_sender_supplied_in_body(
    client, app, create_user, authenticated_user
):
    """The sender is taken from the JWT only; a sender_id field is rejected."""
    victim = create_user(email="victim@example.com", balance="500.00")
    receiver = create_user(email="receiver@example.com", balance="0.00")
    attacker, headers = authenticated_user(email="attacker@example.com", balance="1.00")

    response = client.post(
        TRANSFER_URL,
        headers=headers,
        json={
            "receiver_id": receiver["id"],
            "amount": "100.00",
            "sender_id": victim["id"],
        },
    )

    assert response.status_code == 400

    assert _balance(app, victim["id"]) == Decimal("500.00")
    assert _balance(app, receiver["id"]) == Decimal("0.00")
    assert _balance(app, attacker["id"]) == Decimal("1.00")


def test_transfer_to_nonexistent_receiver_rejected(client, authenticated_user):
    _, headers = authenticated_user(email="sender@example.com", balance="100.00")

    response = client.post(
        TRANSFER_URL, headers=headers, json={"receiver_id": 999999, "amount": "5.00"}
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "RECEIVER_NOT_FOUND"


def test_self_transfer_rejected(client, app, authenticated_user):
    sender, headers = authenticated_user(email="sender@example.com", balance="100.00")

    response = client.post(
        TRANSFER_URL, headers=headers, json={"receiver_id": sender["id"], "amount": "5.00"}
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "SELF_TRANSFER_NOT_ALLOWED"

    assert _balance(app, sender["id"]) == Decimal("100.00")

    with app.app_context():
        assert Transaction.query.count() == 0


def test_transfer_to_frozen_receiver_rejected(
    client, app, create_user, authenticated_user
):
    receiver = create_user(email="frozen@example.com", status="Frozen")
    sender, headers = authenticated_user(email="sender@example.com", balance="100.00")

    response = client.post(
        TRANSFER_URL,
        headers=headers,
        json={"receiver_id": receiver["id"], "amount": "5.00"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "RECEIVER_NOT_ELIGIBLE"

    assert _balance(app, sender["id"]) == Decimal("100.00")


def test_failed_transfer_rolls_back_completely(
    client, app, create_user, authenticated_user, monkeypatch
):
    """If crediting the receiver fails, the sender must not be debited."""
    receiver = create_user(email="receiver@example.com", balance="20.00")
    sender, headers = authenticated_user(email="sender@example.com", balance="100.00")

    def _explode(*args, **kwargs):
        raise RuntimeError("credit failure")

    monkeypatch.setattr(
        "app.services.transaction_service.WalletService.credit", _explode
    )

    response = client.post(
        TRANSFER_URL,
        headers=headers,
        json={"receiver_id": receiver["id"], "amount": "30.00"},
    )

    assert response.status_code == 500
    assert "credit failure" not in response.get_data(as_text=True)

    assert _balance(app, sender["id"]) == Decimal("100.00")
    assert _balance(app, receiver["id"]) == Decimal("20.00")

    with app.app_context():
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


def test_transfer_without_wallet_is_rejected(
    client, app, create_user, authenticated_user
):
    receiver = create_user(email="receiver@example.com")
    sender, headers = authenticated_user(email="sender@example.com", balance="50.00")

    with app.app_context():
        db.session.delete(Wallet.query.filter_by(user_id=sender["id"]).first())
        db.session.commit()

    response = client.post(
        TRANSFER_URL,
        headers=headers,
        json={"receiver_id": receiver["id"], "amount": "5.00"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "WALLET_NOT_FOUND"


# --- history ------------------------------------------------------------


def test_history_requires_authentication(client):
    assert client.get(TRANSACTIONS_URL).status_code == 401


def test_history_returns_only_own_transactions(
    client, create_user, authenticated_user, login
):
    receiver = create_user(email="receiver@example.com", balance="0.00")
    stranger_a = create_user(email="stranger-a@example.com", balance="100.00")
    stranger_b = create_user(email="stranger-b@example.com", balance="0.00")

    sender, sender_headers = authenticated_user(
        email="sender@example.com", balance="100.00"
    )

    client.post(
        TRANSFER_URL,
        headers=sender_headers,
        json={"receiver_id": receiver["id"], "amount": "10.00", "note": "mine"},
    )

    stranger_headers = login(stranger_a["email"], stranger_a["password"])
    client.post(
        TRANSFER_URL,
        headers=stranger_headers,
        json={"receiver_id": stranger_b["id"], "amount": "20.00", "note": "not mine"},
    )

    response = client.get(TRANSACTIONS_URL, headers=sender_headers)

    assert response.status_code == 200

    data = response.get_json()["data"]
    transactions = data["transactions"]

    assert len(transactions) == 1
    assert transactions[0]["note"] == "mine"
    assert transactions[0]["direction"] == "out"

    notes = [transaction["note"] for transaction in transactions]

    assert "not mine" not in notes

    assert data["pagination"]["total"] == 1
    assert data["pagination"]["page"] == 1


def test_receiver_sees_incoming_transaction(client, create_user, authenticated_user, login):
    receiver = create_user(email="receiver@example.com", balance="0.00")
    _, sender_headers = authenticated_user(email="sender@example.com", balance="100.00")

    client.post(
        TRANSFER_URL,
        headers=sender_headers,
        json={"receiver_id": receiver["id"], "amount": "10.00"},
    )

    receiver_headers = login(receiver["email"], receiver["password"])

    response = client.get(TRANSACTIONS_URL, headers=receiver_headers)

    transactions = response.get_json()["data"]["transactions"]

    assert len(transactions) == 1
    assert transactions[0]["direction"] == "in"


def test_history_pagination(client, create_user, authenticated_user):
    receiver = create_user(email="receiver@example.com", balance="0.00")
    _, headers = authenticated_user(email="sender@example.com", balance="100.00")

    for _ in range(3):
        client.post(
            TRANSFER_URL,
            headers=headers,
            json={"receiver_id": receiver["id"], "amount": "1.00"},
        )

    response = client.get(
        f"{TRANSACTIONS_URL}?page=1&per_page=2", headers=headers
    )

    data = response.get_json()["data"]

    assert len(data["transactions"]) == 2
    assert data["pagination"]["total"] == 3
    assert data["pagination"]["pages"] == 2
    assert data["pagination"]["has_next"] is True

    second_page = client.get(
        f"{TRANSACTIONS_URL}?page=2&per_page=2", headers=headers
    )

    assert len(second_page.get_json()["data"]["transactions"]) == 1


def test_history_rejects_invalid_pagination(client, authenticated_user):
    _, headers = authenticated_user(email="sender@example.com")

    assert client.get(f"{TRANSACTIONS_URL}?page=0", headers=headers).status_code == 400
    assert (
        client.get(f"{TRANSACTIONS_URL}?per_page=abc", headers=headers).status_code
        == 400
    )


# --- detail -------------------------------------------------------------


def test_transaction_detail_requires_authentication(client):
    assert client.get(f"{TRANSACTIONS_URL}/1").status_code == 401


def test_participants_can_view_transaction_detail(
    client, create_user, authenticated_user, login
):
    receiver = create_user(email="receiver@example.com", balance="0.00")
    _, sender_headers = authenticated_user(email="sender@example.com", balance="100.00")

    created = client.post(
        TRANSFER_URL,
        headers=sender_headers,
        json={"receiver_id": receiver["id"], "amount": "15.00"},
    )
    transaction_id = created.get_json()["data"]["transaction"]["id"]

    sender_response = client.get(
        f"{TRANSACTIONS_URL}/{transaction_id}", headers=sender_headers
    )

    assert sender_response.status_code == 200
    assert sender_response.get_json()["data"]["transaction"]["direction"] == "out"

    receiver_headers = login(receiver["email"], receiver["password"])
    receiver_response = client.get(
        f"{TRANSACTIONS_URL}/{transaction_id}", headers=receiver_headers
    )

    assert receiver_response.status_code == 200
    assert receiver_response.get_json()["data"]["transaction"]["direction"] == "in"


def test_unrelated_user_cannot_view_transaction_detail(
    client, create_user, authenticated_user
):
    receiver = create_user(email="receiver@example.com", balance="0.00")
    _, sender_headers = authenticated_user(email="sender@example.com", balance="100.00")
    _, stranger_headers = authenticated_user(email="stranger@example.com")

    created = client.post(
        TRANSFER_URL,
        headers=sender_headers,
        json={"receiver_id": receiver["id"], "amount": "15.00"},
    )
    transaction_id = created.get_json()["data"]["transaction"]["id"]

    response = client.get(
        f"{TRANSACTIONS_URL}/{transaction_id}", headers=stranger_headers
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "TRANSACTION_NOT_FOUND"


def test_missing_transaction_detail_returns_not_found(client, authenticated_user):
    _, headers = authenticated_user(email="sender@example.com")

    response = client.get(f"{TRANSACTIONS_URL}/999999", headers=headers)

    assert response.status_code == 404


def test_transaction_history_avoids_nplus1(client, app, authenticated_user):
    """History must not lazy-load sender/receiver per row (R7)."""
    from sqlalchemy import event

    user, headers = authenticated_user(email="history@example.com", balance="0.00")

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        balance = Decimal("0.00")
        for i in range(20):
            tx = Transaction(
                tx_code=f"VYLNPLUS{i:08d}",
                sender_id=None,
                receiver_id=user["id"],
                amount=Decimal("1.00"),
                fee=Decimal("0.00"),
                status=TransactionStatus.COMPLETED,
                tx_type=TransactionType.DEPOSIT,
            )
            db.session.add(tx)
            db.session.flush()
            balance_before = balance
            balance = balance + Decimal("1.00")
            db.session.add(
                WalletLedger(
                    wallet_id=wallet.id,
                    transaction_id=tx.id,
                    entry_type=LedgerEntryType.CREDIT,
                    amount=Decimal("1.00"),
                    balance_before=balance_before,
                    balance_after=balance,
                    reference=tx.tx_code,
                    description="deposit",
                )
            )
        wallet.balance = balance
        db.session.commit()

    select_count = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            select_count.append(statement)

    bind = db.session.get_bind()
    event.listen(bind, "before_cursor_execute", _count)
    try:
        response = client.get(f"{TRANSACTIONS_URL}?per_page=100", headers=headers)
    finally:
        event.remove(bind, "before_cursor_execute", _count)

    assert response.status_code == 200
    # With eager loading the SELECT count is constant; without it the count
    # would scale with the 20 rows (one lazy load per row).
    assert len(select_count) < 10
