"""Tests for the M-Pesa (Daraja) deposit flow.

No test performs a real Daraja request: the outbound HTTP calls made by
``app.services.mpesa_service`` are replaced with fakes.
"""

import os
import threading
from decimal import Decimal

import pytest
import requests
from sqlalchemy import event

from app.extensions import db
from app.models import (
    LedgerEntryType,
    MpesaTransaction,
    MpesaTransactionStatus,
    Transaction,
    TransactionType,
    Wallet,
    WalletLedger,
)
from app.schemas.mpesa_schema import parse_stk_callback
from app.services.mpesa_service import MpesaService
from app.services.wallet_service import WalletService
from app.utils.errors import ApiError, ErrorCode

STK_PUSH_URL = "/api/mpesa/stk-push"
CALLBACK_URL = "/api/mpesa/callback"

CHECKOUT_REQUEST_ID = "ws_CO_20260823010101123456"
MERCHANT_REQUEST_ID = "29115-34620561-1"
RECEIPT_NUMBER = "QK12AB34CD"

# ``with_for_update()`` is silently ignored by SQLite, so the callback/recovery
# concurrency regression can only be verified against PostgreSQL. Point
# ``TEST_DATABASE_URL`` at a disposable PostgreSQL database to run it.
_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
_POSTGRES_TEST_DB = _TEST_DATABASE_URL.startswith(
    ("postgresql://", "postgresql+", "postgres://")
)

requires_postgres = pytest.mark.skipif(
    not _POSTGRES_TEST_DB,
    reason=(
        "row locking is a no-op on SQLite; set TEST_DATABASE_URL to a "
        "non-production PostgreSQL database to run this concurrency regression"
    ),
)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FakeDaraja:
    """Minimal stand-in for the ``requests`` module used by the service.

    The STK Push (``stkpush``) and the server-side reconciliation query
    (``stkpushquery``) are distinct endpoints; the query result is what the
    service trusts, so the fake lets each be controlled independently.
    """

    # The service catches these, so the fake must expose the real classes.
    RequestException = requests.RequestException
    ConnectionError = requests.ConnectionError
    HTTPError = requests.HTTPError

    def __init__(
        self,
        stk_response=None,
        token_response=None,
        raise_on_push=False,
        query_response=None,
        raise_on_query=False,
    ):
        self.stk_response = stk_response or {
            "MerchantRequestID": MERCHANT_REQUEST_ID,
            "CheckoutRequestID": CHECKOUT_REQUEST_ID,
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing",
            "CustomerMessage": "Success. Request accepted for processing",
        }
        self.token_response = token_response or {"access_token": "fake-token"}
        self.raise_on_push = raise_on_push
        # The reconciliation query is what authorises a credit.
        self.query_response = query_response or {
            "ResultCode": "0",
            "ResultDesc": "The service request is processed successfully.",
        }
        self.raise_on_query = raise_on_query
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))

        return _FakeResponse(self.token_response)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))

        if "stkpushquery" in url:
            if self.raise_on_query:
                raise requests.ConnectionError("network down")
            return _FakeResponse(self.query_response)

        if self.raise_on_push:
            raise requests.ConnectionError("network down")

        return _FakeResponse(self.stk_response)


@pytest.fixture
def fake_daraja(monkeypatch):
    def _install(
        stk_response=None,
        token_response=None,
        raise_on_push=False,
        query_response=None,
        raise_on_query=False,
    ):
        fake = _FakeDaraja(
            stk_response=stk_response,
            token_response=token_response,
            raise_on_push=raise_on_push,
            query_response=query_response,
            raise_on_query=raise_on_query,
        )
        monkeypatch.setattr("app.services.mpesa_service.requests", fake)

        return fake

    return _install


def _callback_payload(
    result_code=0,
    checkout_request_id=CHECKOUT_REQUEST_ID,
    amount=500,
    receipt=RECEIPT_NUMBER,
):
    callback = {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": MERCHANT_REQUEST_ID,
                "CheckoutRequestID": checkout_request_id,
                "ResultCode": result_code,
                "ResultDesc": "The service request is processed successfully."
                if result_code == 0
                else "Request cancelled by user",
            }
        }
    }

    if result_code == 0:
        callback["Body"]["stkCallback"]["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": amount},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": 20260823010203},
                {"Name": "PhoneNumber", "Value": 254712345678},
            ]
        }

    return callback


def _balance(app, user_id):
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()

        return Decimal(str(wallet.balance))


# --- STK push -----------------------------------------------------------


def test_stk_push_requires_authentication(client, fake_daraja):
    fake_daraja()

    response = client.post(STK_PUSH_URL, json={"amount": "500", "phone": "0712345678"})

    assert response.status_code == 401


def test_stk_push_validates_input(client, authenticated_user, fake_daraja):
    fake = fake_daraja()
    _, headers = authenticated_user(email="depositor@example.com")

    invalid_payloads = [
        {},
        {"amount": "500"},
        {"phone": "0712345678"},
        {"amount": "0", "phone": "0712345678"},
        {"amount": "-100", "phone": "0712345678"},
        {"amount": "abc", "phone": "0712345678"},
        {"amount": "10.50", "phone": "0712345678"},
        {"amount": "500", "phone": "12345"},
        {"amount": "500", "phone": "0812345678"},
        {"amount": "500", "phone": "0712345678", "user_id": 1},
    ]

    for payload in invalid_payloads:
        response = client.post(STK_PUSH_URL, headers=headers, json=payload)

        assert response.status_code == 400, payload
        assert response.get_json()["success"] is False, payload

    # Nothing was sent to Daraja for invalid requests.
    assert fake.requests == []


@pytest.mark.parametrize(
    "phone", ["0712345678", "+254712345678", "254712345678", "712345678", "0112345678"]
)
def test_stk_push_accepts_kenyan_phone_formats(
    client, app, authenticated_user, fake_daraja, phone
):
    fake = fake_daraja()
    _, headers = authenticated_user(email="depositor@example.com")

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": phone}
    )

    assert response.status_code == 201

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.phone_number.startswith("254")
        assert len(stored.phone_number) == 12

    push_request = next(r for r in fake.requests if r[0] == "POST")

    assert push_request[2]["json"]["PhoneNumber"] == stored.phone_number


def test_successful_stk_push_creates_pending_deposit_without_crediting_wallet(
    client, app, authenticated_user, fake_daraja
):
    fake = fake_daraja()
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
    )

    assert response.status_code == 201

    deposit = response.get_json()["data"]["deposit"]

    assert deposit["checkout_request_id"] == CHECKOUT_REQUEST_ID
    assert deposit["status"] == MpesaTransactionStatus.PENDING
    assert deposit["amount"] == "500.00"

    # An STK request alone must never credit the wallet.
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.user_id == user["id"]
        assert stored.status == MpesaTransactionStatus.PENDING
        assert stored.merchant_request_id == MERCHANT_REQUEST_ID
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0

    # Daraja received a whole-number amount and the configured shortcode.
    push_request = next(r for r in fake.requests if r[0] == "POST")
    sent = push_request[2]["json"]

    assert sent["Amount"] == 500
    assert sent["BusinessShortCode"] == "174379"
    assert sent["AccountReference"] == stored.account_reference


def test_stk_push_response_never_exposes_daraja_secrets(
    client, authenticated_user, fake_daraja
):
    fake_daraja()
    _, headers = authenticated_user(email="depositor@example.com")

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
    )

    body = response.get_data(as_text=True)

    for secret in ("test-passkey", "test-consumer-key", "test-consumer-secret", "Password"):
        assert secret not in body


def test_failed_stk_push_marks_deposit_failed(
    client, app, authenticated_user, fake_daraja
):
    fake_daraja(
        stk_response={
            "requestId": "1234",
            "errorCode": "400.002.02",
            "errorMessage": "Bad Request - Invalid Amount",
        }
    )
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
    )

    assert response.status_code == 502
    assert response.get_json()["error"] == "MPESA_REQUEST_FAILED"

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.FAILED
        assert stored.checkout_request_id is None


def test_stk_push_handles_daraja_network_failure(
    client, app, authenticated_user, fake_daraja
):
    fake_daraja(raise_on_push=True)
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
    )

    assert response.status_code == 502

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        assert MpesaTransaction.query.one().status == MpesaTransactionStatus.FAILED


def test_stk_push_requires_configuration(client, app, authenticated_user, fake_daraja):
    fake_daraja()
    _, headers = authenticated_user(email="depositor@example.com")

    app.config["DARAJA_PASSKEY"] = ""

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "MPESA_NOT_CONFIGURED"


# --- callback -----------------------------------------------------------


def _initiate_deposit(client, headers, fake_daraja, amount="500"):
    fake_daraja()

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": amount, "phone": "0712345678"}
    )

    assert response.status_code == 201

    return response.get_json()["data"]["deposit"]


def test_callback_does_not_require_authentication(
    client, app, authenticated_user, fake_daraja
):
    _, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # No Authorization header is sent.
    response = client.post(CALLBACK_URL, json=_callback_payload())

    assert response.status_code == 200


def test_successful_callback_credits_wallet_once(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    response = client.post(CALLBACK_URL, json=_callback_payload())

    assert response.status_code == 200
    assert response.get_json()["success"] is True

    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        deposit = MpesaTransaction.query.one()

        assert deposit.status == MpesaTransactionStatus.COMPLETED
        assert deposit.mpesa_receipt_number == RECEIPT_NUMBER
        assert deposit.result_code == "0"
        assert deposit.transaction_id is not None

        transaction = Transaction.query.one()

        assert transaction.tx_type == TransactionType.DEPOSIT
        assert transaction.sender_id is None
        assert transaction.receiver_id == user["id"]
        assert Decimal(str(transaction.amount)) == Decimal("500.00")

        entries = WalletLedger.query.all()

        assert len(entries) == 1
        assert entries[0].entry_type == LedgerEntryType.CREDIT
        # The canonical idempotency reference is the Daraja checkout id, which
        # both the callback and the recovery path use, so
        # ``unique_wallet_ledger_reference`` can block a duplicate credit from
        # either path. The receipt is still stored on the deposit row.
        assert entries[0].reference == CHECKOUT_REQUEST_ID
        assert deposit.mpesa_receipt_number == RECEIPT_NUMBER
        assert Decimal(str(entries[0].balance_before)) == Decimal("10.00")
        assert Decimal(str(entries[0].balance_after)) == Decimal("510.00")


def test_duplicate_callback_credits_wallet_exactly_once(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    first = client.post(CALLBACK_URL, json=_callback_payload())
    second = client.post(CALLBACK_URL, json=_callback_payload())
    third = client.post(CALLBACK_URL, json=_callback_payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200

    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        assert MpesaTransaction.query.count() == 1
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1


def test_failed_reconciliation_does_not_credit_wallet(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # The callback claims success, but Daraja's authoritative reconciliation
    # query reports failure, so the wallet must NOT be credited.
    fake_daraja(
        query_response={
            "ResultCode": "1032",
            "ResultDesc": "Request cancelled by user",
        }
    )

    response = client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert response.status_code == 200

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        deposit = MpesaTransaction.query.one()

        assert deposit.status == MpesaTransactionStatus.FAILED
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


def test_callback_after_failed_reconciliation_is_not_reprocessed(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # First attempt: Daraja reconciliation fails -> FAILED.
    fake_daraja(
        query_response={
            "ResultCode": "1032",
            "ResultDesc": "Request cancelled by user",
        }
    )
    client.post(CALLBACK_URL, json=_callback_payload(result_code=1032))

    # A later (genuine) callback must not resurrect the failed deposit even if
    # reconciliation would now succeed: status is already terminal.
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        assert MpesaTransaction.query.one().status == MpesaTransactionStatus.FAILED
        assert Transaction.query.count() == 0


def test_callback_with_mismatched_amount_does_not_credit_wallet(
    client, app, authenticated_user, fake_daraja
):
    """F/Test B: a confirmed payment with a mismatched callback amount.

    Daraja's authoritative query says the payment succeeded, so this is NOT
    evidence of non-payment: the deposit must stay recoverable and uncredited,
    never terminal FAILED.
    """
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja, amount="500")

    # Default fake query returns ResultCode 0 (payment confirmed).
    response = client.post(CALLBACK_URL, json=_callback_payload(amount=100000))

    assert response.status_code == 200

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.RECONCILIATION_PENDING
        assert stored.failure_reason is not None
        assert "mismatch" in stored.failure_reason.lower()
        assert stored.reconciliation_attempts >= 1
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


def test_callback_for_unknown_checkout_id_is_ignored(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    response = client.post(
        CALLBACK_URL, json=_callback_payload(checkout_request_id="ws_CO_unknown")
    )

    assert response.status_code == 200

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        assert MpesaTransaction.query.one().status == MpesaTransactionStatus.PENDING
        assert Transaction.query.count() == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"Body": {}},
        {"Body": {"stkCallback": {}}},
        {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1"}}},
        {"Body": "not-a-dict"},
    ],
)
def test_malformed_callback_payload_rejected(client, payload):
    response = client.post(CALLBACK_URL, json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"] == "INVALID_CALLBACK_PAYLOAD"


def test_deposit_appears_in_transaction_history(
    client, app, authenticated_user, fake_daraja
):
    _, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)
    client.post(CALLBACK_URL, json=_callback_payload())

    response = client.get("/api/transactions", headers=headers)

    transactions = response.get_json()["data"]["transactions"]

    assert len(transactions) == 1
    assert transactions[0]["tx_type"] == TransactionType.DEPOSIT
    assert transactions[0]["direction"] == "in"
    assert transactions[0]["sender"] is None


# --- R1: callback must not be forgeable --------------------------------


def test_forged_success_callback_without_daraja_confirmation_does_not_credit(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    deposit = _initiate_deposit(client, headers, fake_daraja)

    # An attacker who knows their own CheckoutRequestID (returned by the STK
    # push) forges a success callback with a valid receipt, but Daraja's
    # server-side reconciliation query does not confirm the payment.
    fake_daraja(
        query_response={
            "ResultCode": "1032",
            "ResultDesc": "Request cancelled by user",
        }
    )

    response = client.post(
        CALLBACK_URL,
        json=_callback_payload(
            checkout_request_id=deposit["checkout_request_id"],
            result_code=0,
            amount=500,
            receipt=RECEIPT_NUMBER,
        ),
    )

    # The endpoint still acknowledges so Safaricom does not retry, but no money
    # moves.
    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.FAILED
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


def test_correct_checkout_id_alone_is_insufficient_to_credit(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    deposit = _initiate_deposit(client, headers, fake_daraja)

    # Even when the callback carries the exact checkout id and a matching
    # amount/receipt, a failing Daraja reconciliation blocks the credit.
    fake_daraja(
        query_response={"ResultCode": "1032", "ResultDesc": "cancelled"}
    )

    response = client.post(
        CALLBACK_URL,
        json=_callback_payload(
            checkout_request_id=deposit["checkout_request_id"],
            result_code=0,
            amount=500,
            receipt=RECEIPT_NUMBER,
        ),
    )

    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        assert MpesaTransaction.query.one().status == MpesaTransactionStatus.FAILED


def test_reconciliation_failure_keeps_deposit_recoverable_for_retry(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # Daraja cannot be reached; the deposit must move to RECONCILIATION_PENDING
    # (not fail, not credit) so a later callback retry or reconciliation can
    # still recover it. This is the key remediation of the stranding bug.
    fake_daraja(raise_on_query=True)

    response = client.post(CALLBACK_URL, json=_callback_payload())

    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.RECONCILIATION_PENDING
        assert stored.reconciliation_attempts >= 1
        assert stored.last_reconciled_at is not None


def test_genuine_confirmed_reconciliation_credits_wallet(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # Default fake returns a successful Daraja reconciliation.
    response = client.post(CALLBACK_URL, json=_callback_payload())

    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        assert MpesaTransaction.query.one().status == MpesaTransactionStatus.COMPLETED


# --- R2: recovery of pending deposits -----------------------------------


def test_reconcile_pending_credits_confirmed_deposit(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        mpesa_transaction = MpesaTransaction(
            user_id=user["id"],
            wallet_id=wallet.id,
            account_reference="REFRECOVER",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.PENDING,
            checkout_request_id="ws_CO_recover_1",
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    summary = MpesaService.reconcile_pending()

    assert summary["credited"] == 1
    assert summary["failed"] == 0
    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_recover_1"
        ).first()

        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1


def test_reconcile_pending_keeps_unconfirmed_deposit_pending(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    fake_daraja(query_response={"ResultCode": "1032", "ResultDesc": "cancelled"})

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        mpesa_transaction = MpesaTransaction(
            user_id=user["id"],
            wallet_id=wallet.id,
            account_reference="REFRECOVER",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.PENDING,
            checkout_request_id="ws_CO_recover_2",
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    summary = MpesaService.reconcile_pending()

    assert summary["failed"] == 1
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_recover_2"
        ).first()

        assert stored.status == MpesaTransactionStatus.FAILED
        assert Transaction.query.count() == 0


# --- R1: optional callback source allowlist (defence-in-depth) ---------


def test_callback_rejects_unauthorized_source_when_allowlist_configured(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # The test client connects from 127.0.0.1, which is not in the allowlist.
    app.config["DARAJA_CALLBACK_ALLOWED_IPS"] = ["203.0.113.5"]

    response = client.post(CALLBACK_URL, json=_callback_payload())

    assert response.status_code == 403
    assert response.get_json()["error"] == "FORBIDDEN"
    assert _balance(app, user["id"]) == Decimal("10.00")


def test_callback_allowed_when_source_matches_allowlist(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    app.config["DARAJA_CALLBACK_ALLOWED_IPS"] = ["127.0.0.1"]

    response = client.post(CALLBACK_URL, json=_callback_payload())

    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("510.00")


# --- RECONCILIATION_PENDING remediation --------------------------------


def test_incident_regression_inconclusive_query_is_recoverable_not_failed(
    client, app, authenticated_user, fake_daraja
):
    """B: callback success + inconclusive query must NOT become FAILED.

    This is the exact stranding bug. The deposit must land in
    RECONCILIATION_PENDING, the wallet must be untouched, and no
    Transaction/WalletLedger must be created.
    """
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    fake_daraja(
        query_response={
            "ResultCode": "9999",
            "ResultDesc": "Inconclusive / unknown result",
        }
    )

    response = client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.RECONCILIATION_PENDING
        # Observability fields are persisted.
        assert stored.query_result_code == "9999"
        assert stored.reconciliation_attempts == 1
        assert stored.last_reconciled_at is not None
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


def test_unknown_nonzero_query_response_stays_recoverable(
    client, app, authenticated_user, fake_daraja
):
    """G: an unrecognised non-zero query code must never auto-become FAILED."""
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    fake_daraja(
        query_response={
            "ResultCode": "1037",
            "ResultDesc": "DS timeout (treated as inconclusive here)",
        }
    )

    client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.RECONCILIATION_PENDING
        assert stored.query_result_code == "1037"
        assert Transaction.query.count() == 0


def test_genuine_cancellation_marks_failed_no_credit(
    client, app, authenticated_user, fake_daraja
):
    """F: the documented definitive cancellation (1032) becomes FAILED."""
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    fake_daraja(
        query_response={
            "ResultCode": "1032",
            "ResultDesc": "Request cancelled by user",
        }
    )

    response = client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.FAILED
        assert stored.failure_reason == "Request cancelled by user"
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


def test_reconciliation_pending_is_recoverable_via_recovery(
    client, app, authenticated_user, fake_daraja
):
    """C: first reconciliation inconclusive, later reconciliation succeeds."""
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        mpesa_transaction = MpesaTransaction(
            user_id=user["id"],
            wallet_id=wallet.id,
            account_reference="REFRECOVER",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.RECONCILIATION_PENDING,
            checkout_request_id="ws_CO_recover_3",
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    # First recovery attempt: inconclusive.
    fake_daraja(
        query_response={
            "ResultCode": "9999",
            "ResultDesc": "Inconclusive",
        }
    )
    summary = MpesaService.recover_deposits()
    assert summary["reconciliation_pending"] == 1
    assert summary["credited"] == 0
    assert _balance(app, user["id"]) == Decimal("10.00")

    # Later recovery attempt: Daraja confirms success.
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    summary = MpesaService.recover_deposits()
    assert summary["credited"] == 1

    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_recover_3"
        ).first()

        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1


def test_recovery_timeout_keeps_deposit_recoverable(
    client, app, authenticated_user, fake_daraja
):
    """H: a Daraja query timeout/API error leaves the deposit recoverable."""
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        mpesa_transaction = MpesaTransaction(
            user_id=user["id"],
            wallet_id=wallet.id,
            account_reference="REFRECOVER",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.RECONCILIATION_PENDING,
            checkout_request_id="ws_CO_recover_4",
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    fake_daraja(raise_on_query=True)
    summary = MpesaService.recover_deposits()

    assert summary["reconciliation_pending"] == 1
    assert summary["credited"] == 0
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_recover_4"
        ).first()

        assert stored.status == MpesaTransactionStatus.RECONCILIATION_PENDING
        assert stored.reconciliation_attempts >= 1


def test_duplicate_recovery_does_not_double_credit(
    client, app, authenticated_user, fake_daraja
):
    """E: running recovery repeatedly against a completed deposit credits once."""
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        mpesa_transaction = MpesaTransaction(
            user_id=user["id"],
            wallet_id=wallet.id,
            account_reference="REFRECOVER",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.PENDING,
            checkout_request_id="ws_CO_recover_5",
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    first = MpesaService.recover_deposits()
    second = MpesaService.recover_deposits()
    third = MpesaService.recover_deposits()

    assert first["credited"] == 1
    assert second["credited"] == 0
    assert third["credited"] == 0

    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1


def test_reconciliation_pending_reprocessed_by_callback_recovers(
    client, app, authenticated_user, fake_daraja
):
    """A RECONCILIATION_PENDING deposit can be completed by a later callback."""
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # First callback: inconclusive query -> RECONCILIATION_PENDING.
    fake_daraja(query_response={"ResultCode": "9999", "ResultDesc": "Inconclusive"})
    client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert _balance(app, user["id"]) == Decimal("10.00")
    with app.app_context():
        assert (
            MpesaTransaction.query.one().status
            == MpesaTransactionStatus.RECONCILIATION_PENDING
        )

    # Later callback: Daraja now confirms success.
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert _balance(app, user["id"]) == Decimal("510.00")
    with app.app_context():
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1


# --- user status + admin recovery endpoints -----------------------------


def test_user_can_fetch_own_mpesa_transaction_status(
    client, app, authenticated_user, fake_daraja
):
    _, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    deposit = _initiate_deposit(client, headers, fake_daraja)

    response = client.get(
        f"/api/mpesa/transactions/{deposit['id']}", headers=headers
    )

    assert response.status_code == 200
    body = response.get_json()["data"]["transaction"]

    assert body["id"] == deposit["id"]
    assert body["status"] == MpesaTransactionStatus.PENDING
    # checkout_request_id must NOT be returned; phone must be masked.
    assert "checkout_request_id" not in body
    assert body["phone_number"].startswith("****")
    assert len(body["phone_number"]) <= 8


def test_user_cannot_fetch_another_users_mpesa_transaction(
    client, app, authenticated_user, fake_daraja, create_user, login
):
    owner, owner_headers = authenticated_user(
        email="owner@example.com", balance="10.00"
    )
    deposit = _initiate_deposit(client, owner_headers, fake_daraja)

    other = create_user(email="other@example.com", balance="10.00")
    other_headers = login(other["email"], other["password"])

    # Client-facing id is the transaction primary key (scoped to owner).
    response = client.get(
        f"/api/mpesa/transactions/{deposit['id']}", headers=other_headers
    )

    # Reported as not found so the existence of another user's deposit is hidden.
    assert response.status_code == 404


def test_unauthenticated_status_request_is_rejected(client, fake_daraja):
    fake_daraja()
    response = client.get("/api/mpesa/transactions/1")

    assert response.status_code == 401


def test_admin_reconcile_runs_recovery_safely(
    client, app, authenticated_user, fake_daraja, create_user, login
):
    admin = create_user(email="admin@example.com", role="admin", balance="0.00")
    admin_headers = login(admin["email"], admin["password"])

    user, _ = authenticated_user(email="depositor@example.com", balance="10.00")
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user["id"]).first()
        mpesa_transaction = MpesaTransaction(
            user_id=user["id"],
            wallet_id=wallet.id,
            account_reference="REFRECOVER",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.RECONCILIATION_PENDING,
            checkout_request_id="ws_CO_recover_admin",
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    response = client.post("/api/mpesa/admin/reconcile", headers=admin_headers)

    assert response.status_code == 200
    summary = response.get_json()["data"]["summary"]

    assert summary["credited"] == 1

    # Running again is safe/idempotent: nothing new is credited.
    response2 = client.post("/api/mpesa/admin/reconcile", headers=admin_headers)
    assert response2.get_json()["data"]["summary"]["credited"] == 0

    assert _balance(app, user["id"]) == Decimal("510.00")


def test_admin_reconcile_requires_admin_role(
    client, authenticated_user, fake_daraja
):
    _, headers = authenticated_user(email="depositor@example.com", balance="10.00")

    response = client.post("/api/mpesa/admin/reconcile", headers=headers)

    assert response.status_code == 403


# --- atomicity ----------------------------------------------------------


def test_credit_failure_rolls_back_atomically(
    client, app, authenticated_user, fake_daraja, monkeypatch
):
    """J: a failure during crediting rolls back with no partial state."""
    from sqlalchemy.exc import SQLAlchemyError as _SAErr

    from app.services import wallet_service as _ws

    def _boom(*args, **kwargs):
        raise _SAErr("simulated DB failure")

    monkeypatch.setattr(_ws.WalletService, "credit", _boom)

    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    # The endpoint returns an error but must not persist any partial credit.
    response = client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    assert response.status_code == 500
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        # No transaction or ledger was committed; the deposit remains pending
        # (rolled back to its pre-callback state).
        stored = MpesaTransaction.query.one()

        assert stored.status == MpesaTransactionStatus.PENDING
        assert Transaction.query.count() == 0
        assert WalletLedger.query.count() == 0


# --- PART C/A: double-credit regression (terminal guard, two sessions) -----


def _make_pending_deposit(app, user_id, checkout_request_id, amount="500.00"):
    """Create a PENDING M-Pesa deposit row directly (no Daraja call)."""
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        mpesa_transaction = MpesaTransaction(
            user_id=user_id,
            wallet_id=wallet.id,
            account_reference="REFRECOVER",
            phone_number="254712345678",
            amount=Decimal(amount),
            status=MpesaTransactionStatus.PENDING,
            merchant_request_id="29115-34620561-1",
            checkout_request_id=checkout_request_id,
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    return checkout_request_id


def test_credit_confirmed_deposit_refuses_already_terminal(
    app, create_user
):
    """PART C direct regression: crediting an already-terminal M-Pesa deposit
    (Completed *or* Failed) must never add a wallet credit, Transaction, or
    ledger entry. This is the deepest defence-in-depth guard: even a caller
    holding stale state cannot re-credit a finished deposit.
    """
    user = create_user(email="terminal@example.com", balance="1000.00")
    user_id = user["id"]

    for terminal_status in (
        MpesaTransactionStatus.COMPLETED,
        MpesaTransactionStatus.FAILED,
    ):
        cid = f"ws_CO_term_{terminal_status}"
        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user_id).first()
            mpesa_transaction = MpesaTransaction(
                user_id=user_id,
                wallet_id=wallet.id,
                account_reference="REFTERM",
                phone_number="254712345678",
                amount=Decimal("500.00"),
                status=terminal_status,
                merchant_request_id="29115-34620561-1",
                checkout_request_id=cid,
            )
            db.session.add(mpesa_transaction)
            db.session.commit()

            before_balance = Decimal(
                str(Wallet.query.filter_by(user_id=user_id).first().balance)
            )

        # Re-read in a fresh session so the object is bound (and not expired by
        # the previous commit) before exercising the credit path.
        with app.app_context():
            mtx = MpesaTransaction.query.filter_by(
                checkout_request_id=cid
            ).first()
            result = MpesaService._credit_confirmed_deposit(
                mtx,
                callback_amount=500,
                receipt_number="QKTERM99",
            )
            assert result.status == terminal_status
            # Nothing was created and nothing was credited.
            assert Transaction.query.count() == 0
            assert WalletLedger.query.count() == 0
            after_balance = Decimal(
                str(Wallet.query.filter_by(user_id=user_id).first().balance)
            )

        assert after_balance == before_balance


def test_separate_sessions_callback_then_recovery_single_credit(
    app, create_user, fake_daraja
):
    """PART C companion regression, runnable on SQLite: the callback (its own
    database session/transaction) credits and commits, then the recovery sweep
    (a *different* session) re-reads the deposit under its terminal-state guard
    and skips it. This proves the cross-session re-check prevents a double
    credit even without true lock contention, and executes everywhere.
    """
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    user = create_user(email="twosession@example.com", balance="1000.00")
    user_id = user["id"]
    cid = "ws_CO_twosession"
    _make_pending_deposit(app, user_id, cid)

    def run_callback():
        with app.app_context():
            parsed = parse_stk_callback(
                _callback_payload(result_code=0, checkout_request_id=cid)
            )
            MpesaService.process_callback(parsed)

    cb_thread = threading.Thread(target=run_callback)
    cb_thread.start()
    cb_thread.join(timeout=15)

    summary = MpesaService.recover_deposits()

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        assert Decimal(str(wallet.balance)) == Decimal("1500.00")
        stored = MpesaTransaction.query.filter_by(checkout_request_id=cid).first()
        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1

    # The row is already COMPLETED, so it is no longer a recovery candidate;
    # the sweep must not credit it a second time.
    assert summary["credited"] == 0
    assert summary["processed"] == 0


@requires_postgres
def test_concurrent_callback_and_recovery_single_credit(
    app, create_user, monkeypatch
):
    """PART C/D MANDATORY regression: a callback and a recovery sweep racing on
    the same deposit must produce exactly ONE wallet credit, ONE ledger entry,
    ONE Transaction, and a single COMPLETED state.

    Two real, separate database sessions are used (one per thread; Flask-
    SQLAlchemy's ``db.session`` is thread-local). The recovery thread issues its
    authenticated Daraja query (ResultCode 0) and then *waits* for the callback
    to finish its credit+commit before taking the row lock. Under PostgreSQL
    ``SELECT ... FOR UPDATE`` serialises the two workers: the recovery thread
    blocks on the row lock until the callback commits, then re-reads the now
    COMPLETED status and skips. The deposit is therefore credited exactly once.

    Skipped on SQLite because ``with_for_update()`` is a no-op there and the
    true lock-based race cannot be exercised; set ``TEST_DATABASE_URL`` to a
    disposable PostgreSQL database to run it.
    """
    import threading

    user = create_user(email="race@example.com", balance="1000.00")
    user_id = user["id"]
    cid = "ws_CO_race_1"
    _make_pending_deposit(app, user_id, cid)

    recovered_queried = threading.Event()
    callback_committed = threading.Event()
    result_holder = {}

    recovery_thread = None

    def fake_query(checkout_request_id):
        if threading.current_thread() is recovery_thread:
            # Recovery has sent its Daraja query (ResultCode 0) but has not yet
            # taken the row lock. Signal the harness and wait for the callback to
            # finish its credit + commit before continuing.
            recovered_queried.set()
            callback_committed.wait(timeout=15)
            return {"ResultCode": "0", "ResultDesc": "Success."}
        return {"ResultCode": "0", "ResultDesc": "Success."}

    # Bypass the network entirely; the query result is what authorises a credit,
    # so the fake returns the same confirmed-success payload both threads trust.
    monkeypatch.setattr(
        MpesaService, "query_stk_status", staticmethod(fake_query)
    )

    def run_recovery():
        with app.app_context():
            result_holder["summary"] = MpesaService.recover_deposits()

    def run_callback():
        with app.app_context():
            parsed = parse_stk_callback(
                _callback_payload(result_code=0, checkout_request_id=cid)
            )
            MpesaService.process_callback(parsed)

    recovery_thread = threading.Thread(target=run_recovery, name="recovery")
    recovery_thread.start()
    # Wait until recovery has issued its Daraja query (before the row lock).
    assert recovered_queried.wait(timeout=15)

    callback_thread = threading.Thread(target=run_callback, name="callback")
    callback_thread.start()
    callback_thread.join(timeout=15)
    # Release the recovery thread now that the callback has committed.
    callback_committed.set()
    recovery_thread.join(timeout=15)

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        assert Decimal(str(wallet.balance)) == Decimal("1500.00")
        stored = MpesaTransaction.query.filter_by(checkout_request_id=cid).first()
        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1

    # Recovery explicitly did NOT credit; it skipped the completed row.
    assert result_holder["summary"]["credited"] == 0
    assert result_holder["summary"]["skipped"] == 1


# --- PART G: recovery sweep must isolate one failing row --------------------


def test_recovery_sweep_isolates_row_failure(
    app, create_user, fake_daraja, monkeypatch
):
    """PART G regression: a single row whose credit raises must NOT abort the
    sweep or roll back the rows that already committed. The remaining rows still
    process; the failing row stays recoverable (never terminal, never credited).
    """
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    user = create_user(email="sweep@example.com", balance="1000.00")
    user_id = user["id"]

    cids = ["ws_CO_sweep_a", "ws_CO_sweep_bad", "ws_CO_sweep_c"]
    for cid in cids:
        _make_pending_deposit(app, user_id, cid)

    orig_credit = MpesaService._credit_confirmed_deposit

    def _boom(mpesa_transaction, callback_amount=None, receipt_number=None):
        if mpesa_transaction.checkout_request_id == "ws_CO_sweep_bad":
            # Simulated downstream failure for exactly one deposit.
            raise ApiError(
                "simulated downstream failure",
                500,
                ErrorCode.MPESA_REQUEST_FAILED,
            )
        return orig_credit(
            mpesa_transaction,
            callback_amount=callback_amount,
            receipt_number=receipt_number,
        )

    monkeypatch.setattr(
        MpesaService, "_credit_confirmed_deposit", staticmethod(_boom)
    )

    summary = MpesaService.recover_deposits()

    assert summary["processed"] == 3
    assert summary["credited"] == 2
    assert summary["errors"] == 1

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        # Two successful credits of 500 each, on top of the starting 1000.
        assert Decimal(str(wallet.balance)) == Decimal("2000.00")
        assert Transaction.query.count() == 2
        assert WalletLedger.query.count() == 2

        bad = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_sweep_bad"
        ).first()
        # The bad row is rolled back in isolation and remains recoverable.
        assert not MpesaTransactionStatus.is_terminal(bad.status)

        good = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_sweep_a"
        ).first()
        assert good.status == MpesaTransactionStatus.COMPLETED
