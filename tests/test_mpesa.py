"""Tests for the M-Pesa (Daraja) deposit flow.

No test performs a real Daraja request: the outbound HTTP calls made by
``app.services.mpesa_service`` are replaced with fakes.
"""

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
from app.services.mpesa_service import MpesaService

STK_PUSH_URL = "/api/mpesa/stk-push"
CALLBACK_URL = "/api/mpesa/callback"

CHECKOUT_REQUEST_ID = "ws_CO_20260823010101123456"
MERCHANT_REQUEST_ID = "29115-34620561-1"
RECEIPT_NUMBER = "QK12AB34CD"


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
        assert entries[0].reference == RECEIPT_NUMBER
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
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja, amount="500")

    response = client.post(CALLBACK_URL, json=_callback_payload(amount=100000))

    assert response.status_code == 200

    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        assert MpesaTransaction.query.one().status == MpesaTransactionStatus.FAILED
        assert Transaction.query.count() == 0


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


def test_reconciliation_failure_keeps_deposit_pending_for_retry(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # Daraja cannot be reached; the deposit must stay PENDING (not fail, not
    # credit) so a later callback retry can still reconcile it.
    fake_daraja(raise_on_query=True)

    response = client.post(CALLBACK_URL, json=_callback_payload())

    assert response.status_code == 200
    assert _balance(app, user["id"]) == Decimal("10.00")

    with app.app_context():
        assert MpesaTransaction.query.one().status == MpesaTransactionStatus.PENDING


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
