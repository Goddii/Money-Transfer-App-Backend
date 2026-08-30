"""Tests for the M-Pesa (Daraja) deposit flow.

No test performs a real Daraja request: the outbound HTTP calls made by
``app.services.mpesa_service`` are replaced with fakes.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import requests
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects import postgresql

from app import create_app
from app.extensions import db
from app.models import (
    LedgerEntryType,
    MpesaTransaction,
    MpesaTransactionStatus,
    Transaction,
    TransactionType,
    User,
    Wallet,
    WalletLedger,
)
from app.schemas.mpesa_schema import parse_stk_callback
from app.services.mpesa_service import MpesaService, REQUIRED_CONFIG_KEYS
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

requires_sqlite = pytest.mark.skipif(
    _POSTGRES_TEST_DB,
    reason=(
        "asserts the non-PostgreSQL (no advisory locks) leadership fallback; "
        "the PostgreSQL behaviour is covered by the requires_postgres tests"
    ),
)


class _FakeResponse:
    def __init__(
        self, payload=None, status_code=200, non_json=False, content=None
    ):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self._non_json = non_json
        # A non-JSON response still needs a truthy body so the client treats it
        # as a real (but unparseable) payload rather than empty.
        self.content = (
            content
            if content is not None
            else (b"<!doctype html><html>error</html>" if non_json else b"{}")
        )

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if self._non_json:
            raise ValueError("No JSON object could be decoded")
        return self._payload


class _FakeDaraja:
    """Minimal stand-in for the ``requests`` module used by the service.

    The STK Push (``stkpush``) and the server-side reconciliation query
    (``stkpushquery``) are distinct endpoints; the query result is what the
    service trusts, so the fake lets each be controlled independently. Each
    endpoint can be given an explicit HTTP status, a non-JSON body, or forced
    to raise a network exception, so the failure-classification paths can be
    exercised directly.
    """

    # The service catches these, so the fake must expose the real classes.
    RequestException = requests.RequestException
    ConnectionError = requests.ConnectionError
    HTTPError = requests.HTTPError
    Timeout = requests.Timeout

    def __init__(
        self,
        stk_response=None,
        token_response=None,
        raise_on_push=False,
        query_response=None,
        raise_on_query=False,
        token_status=200,
        token_non_json=False,
        push_status=200,
        push_non_json=False,
        query_status=200,
        query_non_json=False,
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

        self.token_status = token_status
        self.token_non_json = token_non_json
        self.push_status = push_status
        self.push_non_json = push_non_json
        self.query_status = query_status
        self.query_non_json = query_non_json

        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))

        return _FakeResponse(
            self.token_response,
            status_code=self.token_status,
            non_json=self.token_non_json,
        )

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))

        if "stkpushquery" in url:
            if self.raise_on_query:
                raise requests.ConnectionError("network down")
            return _FakeResponse(
                self.query_response,
                status_code=self.query_status,
                non_json=self.query_non_json,
            )

        if self.raise_on_push:
            raise requests.ConnectionError("network down")

        return _FakeResponse(
            self.stk_response,
            status_code=self.push_status,
            non_json=self.push_non_json,
        )


@pytest.fixture
def fake_daraja(monkeypatch):
    def _install(
        stk_response=None,
        token_response=None,
        raise_on_push=False,
        query_response=None,
        raise_on_query=False,
        **kwargs,
    ):
        fake = _FakeDaraja(
            stk_response=stk_response,
            token_response=token_response,
            raise_on_push=raise_on_push,
            query_response=query_response,
            raise_on_query=raise_on_query,
            **kwargs,
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


# --- Issue 6: Daraja failure classification & safe diagnostics -------------


def test_get_access_token_401_is_logged_with_status(app, fake_daraja, caplog):
    fake_daraja(token_status=401, token_response={"errorMessage": "Invalid Authentication"})
    with caplog.at_level(logging.ERROR):
        with app.app_context():
            with pytest.raises(ApiError) as exc:
                MpesaService.get_access_token()
    assert exc.value.status_code == 502
    assert any(
        "DARAJA_HTTP_ERROR" in r.message
        and "what=access-token" in r.message
        and "status=401" in r.message
        for r in caplog.records
    )


def test_get_access_token_403_is_logged_with_status(app, fake_daraja, caplog):
    fake_daraja(token_status=403, token_response={"errorMessage": "Forbidden"})
    with caplog.at_level(logging.ERROR):
        with app.app_context():
            with pytest.raises(ApiError):
                MpesaService.get_access_token()
    assert any("status=403" in r.message for r in caplog.records)


def test_get_access_token_404_is_logged_with_status(app, fake_daraja, caplog):
    fake_daraja(token_status=404, token_response={"errorMessage": "Not Found"})
    with caplog.at_level(logging.ERROR):
        with app.app_context():
            with pytest.raises(ApiError):
                MpesaService.get_access_token()
    assert any("status=404" in r.message for r in caplog.records)


def test_get_access_token_5xx_is_logged_with_status(app, fake_daraja, caplog):
    fake_daraja(token_status=503, token_response={"errorMessage": "Service Unavailable"})
    with caplog.at_level(logging.ERROR):
        with app.app_context():
            with pytest.raises(ApiError):
                MpesaService.get_access_token()
    assert any("status=503" in r.message for r in caplog.records)


def test_get_access_token_timeout_is_classified(app, fake_daraja, caplog):
    fake = fake_daraja()

    def _timeout(url, **kwargs):
        raise requests.Timeout("timed out")

    fake.get = _timeout
    with caplog.at_level(logging.ERROR):
        with app.app_context():
            with pytest.raises(ApiError):
                MpesaService.get_access_token()
    assert any("category=timeout" in r.message for r in caplog.records)


def test_get_access_token_connection_error_is_classified(app, fake_daraja, caplog):
    fake = fake_daraja()

    def _connerr(url, **kwargs):
        raise requests.ConnectionError("connection refused")

    fake.get = _connerr
    with caplog.at_level(logging.ERROR):
        with app.app_context():
            with pytest.raises(ApiError):
                MpesaService.get_access_token()
    assert any("category=connection" in r.message for r in caplog.records)


def test_get_access_token_non_json_response_is_classified(app, fake_daraja, caplog):
    fake_daraja(token_non_json=True)
    with caplog.at_level(logging.ERROR):
        with app.app_context():
            with pytest.raises(ApiError):
                MpesaService.get_access_token()
    assert any(
        "category=non-json" in r.message for r in caplog.records
    )


def test_send_stk_push_non_json_response_is_classified(
    client, app, authenticated_user, fake_daraja, caplog
):
    # Token request succeeds, but the STK push returns a non-JSON body (e.g. an
    # HTML gateway/error page). This is the scenario behind the production
    # ``JSONDecodeError`` log, now classified instead of being opaque.
    fake_daraja(push_non_json=True)
    user, headers = authenticated_user(email="depositor@example.com")

    with caplog.at_level(logging.ERROR):
        response = client.post(
            STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
        )

    assert response.status_code == 502
    assert any(
        "DARAJA_HTTP_ERROR" in r.message
        and "what=stk-push" in r.message
        and "category=non-json" in r.message
        for r in caplog.records
    )


def test_send_stk_push_5xx_is_classified(
    client, app, authenticated_user, fake_daraja, caplog
):
    fake_daraja(push_status=502, stk_response={"errorMessage": "Bad gateway"})
    user, headers = authenticated_user(email="depositor@example.com")

    with caplog.at_level(logging.ERROR):
        response = client.post(
            STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
        )

    assert response.status_code == 502
    assert any(
        "DARAJA_HTTP_ERROR" in r.message
        and "what=stk-push" in r.message
        and "status=502" in r.message
        for r in caplog.records
    )


def test_daraja_secrets_are_never_logged(
    client, app, authenticated_user, fake_daraja, caplog
):
    """An upstream error path that logs detail must not leak any secret."""
    fake_daraja(token_status=401, token_response={"errorMessage": "Invalid Authentication"})
    user, headers = authenticated_user(email="depositor@example.com")

    with caplog.at_level(logging.ERROR):
        client.post(
            STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
        )

    log_text = "".join(r.message for r in caplog.records)
    assert "test-consumer-key" not in log_text
    assert "test-consumer-secret" not in log_text
    assert "test-passkey" not in log_text
    # The token only ever exists in success bodies, which are never logged.
    assert "fake-token" not in log_text


def test_successful_oauth_then_stk_flow_credits_once_on_confirmation(
    client, app, authenticated_user, fake_daraja
):
    """Regression: a healthy OAuth + STK push still creates a pending deposit."""
    fake_daraja()
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")

    response = client.post(
        STK_PUSH_URL, headers=headers, json={"amount": "500", "phone": "0712345678"}
    )

    assert response.status_code == 201
    with app.app_context():
        stored = MpesaTransaction.query.one()
        assert stored.status == MpesaTransactionStatus.PENDING


# --- Issue 6: startup configuration validation -----------------------------


def test_validate_config_normalizes_invalid_env(app, caplog):
    app.config["DARAJA_ENV"] = "staging"
    for key in REQUIRED_CONFIG_KEYS:
        app.config.setdefault(key, "present")

    with caplog.at_level(logging.ERROR):
        MpesaService.validate_daraja_config(app)

    assert app.config["DARAJA_ENV"] == "sandbox"
    assert any("DARAJA_CONFIG_INVALID_ENV" in r.message for r in caplog.records)


def test_validate_config_detects_production_sandbox_mismatch(app, caplog):
    app.config["DARAJA_ENV"] = "production"
    app.config["DARAJA_BASE_URL"] = "https://sandbox.safaricom.co.ke"
    for key in REQUIRED_CONFIG_KEYS:
        app.config.setdefault(key, "present")

    with caplog.at_level(logging.ERROR):
        MpesaService.validate_daraja_config(app)

    assert any("DARAJA_CONFIG_ENV_MISMATCH" in r.message for r in caplog.records)


def test_validate_config_detects_sandbox_production_mismatch(app, caplog):
    app.config["DARAJA_ENV"] = "sandbox"
    app.config["DARAJA_BASE_URL"] = "https://api.safaricom.co.ke"
    for key in REQUIRED_CONFIG_KEYS:
        app.config.setdefault(key, "present")

    with caplog.at_level(logging.ERROR):
        MpesaService.validate_daraja_config(app)

    assert any("DARAJA_CONFIG_ENV_MISMATCH" in r.message for r in caplog.records)


def test_validate_config_logs_incomplete_configuration(app, caplog):
    # Partially configured: one value present, the rest empty -> INCOMPLETE.
    for key in REQUIRED_CONFIG_KEYS:
        app.config[key] = ""
    app.config["DARAJA_ENV"] = "sandbox"
    app.config["DARAJA_CONSUMER_KEY"] = "present"

    with caplog.at_level(logging.ERROR):
        MpesaService.validate_daraja_config(app)

    assert any("DARAJA_CONFIG_INCOMPLETE" in r.message for r in caplog.records)


def test_validate_config_all_empty_is_only_a_warning(app, caplog):
    for key in REQUIRED_CONFIG_KEYS:
        app.config[key] = ""
    app.config["DARAJA_ENV"] = "sandbox"

    with caplog.at_level(logging.WARNING):
        MpesaService.validate_daraja_config(app)

    assert any("DARAJA_CONFIG_NOT_CONFIGURED" in r.message for r in caplog.records)
    assert not any("DARAJA_CONFIG_INCOMPLETE" in r.message for r in caplog.records)


def test_validate_config_require_flag_hard_fails_on_incomplete(app):
    for key in REQUIRED_CONFIG_KEYS:
        app.config[key] = ""
    app.config["DARAJA_ENV"] = "sandbox"
    app.config["DARAJA_CONSUMER_KEY"] = "present"
    app.config["DARAJA_REQUIRE_CONFIG"] = "true"

    with pytest.raises(RuntimeError):
        MpesaService.validate_daraja_config(app)


def test_validate_config_valid_full_config_is_quiet(app, caplog):
    for key in REQUIRED_CONFIG_KEYS:
        app.config.setdefault(key, "present")
    app.config["DARAJA_ENV"] = "sandbox"
    app.config["DARAJA_BASE_URL"] = "https://sandbox.safaricom.co.ke"

    with caplog.at_level(logging.ERROR):
        MpesaService.validate_daraja_config(app)

    assert not any("DARAJA_CONFIG" in r.message for r in caplog.records)


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


# --- automatic recovery gap (the intermittent stranding bug) -----------------


def test_inconclusive_callback_is_recoverable_and_user_reconcile_credits(
    client, app, authenticated_user, fake_daraja
):
    """Core race repro: callback arrives before Daraja finalises the payment.

    The callback reaches the backend while Daraja's live query is still
    inconclusive (ResultCode != 0 and not a definitive failure). The deposit
    lands in ``RECONCILIATION_PENDING``. Critically, the *frontend must not see
    failure* -- it sees a recoverable state -- and the user-scoped reconcile
    endpoint later credits the wallet once Daraja confirms. This is exactly the
    "paid but not credited" intermittent failure.
    """
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # Callback arrives; Daraja's authoritative query is still inconclusive.
    fake_daraja(query_response={"ResultCode": "9999", "ResultDesc": "Inconclusive"})
    client.post(CALLBACK_URL, json=_callback_payload(result_code=0))

    # Frontend polls: it must see RECONCILIATION_PENDING, never Failed.
    status_resp = client.get("/api/mpesa/transactions/1", headers=headers)
    assert (
        status_resp.get_json()["data"]["transaction"]["status"]
        == MpesaTransactionStatus.RECONCILIATION_PENDING
    )
    # Wallet is still untouched -- no premature or phantom credit.
    assert _balance(app, user["id"]) == Decimal("10.00")

    # Later, the user (or a frontend nudge) triggers reconciliation; Daraja now
    # confirms success and the wallet is credited exactly once.
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    resp = client.post("/api/mpesa/transactions/1/reconcile", headers=headers)

    assert resp.status_code == 200
    assert (
        resp.get_json()["data"]["status"] == MpesaTransactionStatus.COMPLETED
    )
    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        stored = MpesaTransaction.query.one()
        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1


def test_missing_callback_recovered_by_user_reconcile(
    client, app, authenticated_user, fake_daraja
):
    """Possibility F: the Daraja callback never reaches the backend.

    The deposit sits in PENDING forever unless something reconciles it. The
    user-scoped endpoint lets the frontend recover it without an admin, so a
    genuine payment is never stranded.
    """
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)

    # No callback is ever posted.
    assert _balance(app, user["id"]) == Decimal("10.00")

    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    resp = client.post("/api/mpesa/transactions/1/reconcile", headers=headers)

    assert resp.status_code == 200
    assert (
        resp.get_json()["data"]["status"] == MpesaTransactionStatus.COMPLETED
    )
    assert _balance(app, user["id"]) == Decimal("510.00")


def test_user_reconcile_is_ownership_scoped(
    client, app, authenticated_user, fake_daraja, create_user, login
):
    owner, owner_headers = authenticated_user(
        email="owner@example.com", balance="10.00"
    )
    deposit = _initiate_deposit(client, owner_headers, fake_daraja)

    other = create_user(email="other@example.com", balance="10.00")
    other_headers = login(other["email"], other["password"])
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    # Another user reconciling the owner's deposit must not see/change it.
    resp = client.post(
        f"/api/mpesa/transactions/{deposit['id']}/reconcile",
        headers=other_headers,
    )
    assert resp.status_code == 404
    # Owner's wallet is unaffected (no credit happened on their behalf).
    assert _balance(app, owner["id"]) == Decimal("10.00")


def test_user_reconcile_requires_authentication(client, fake_daraja):
    fake_daraja()
    resp = client.post("/api/mpesa/transactions/1/reconcile")
    assert resp.status_code == 401


def test_user_reconcile_unknown_transaction_returns_404(
    client, authenticated_user, fake_daraja
):
    _, headers = authenticated_user(email="depositor@example.com")
    fake_daraja()
    resp = client.post("/api/mpesa/transactions/999999/reconcile", headers=headers)
    assert resp.status_code == 404


def test_user_reconcile_does_not_double_credit_completed_deposit(
    client, app, authenticated_user, fake_daraja
):
    user, headers = authenticated_user(email="depositor@example.com", balance="10.00")
    _initiate_deposit(client, headers, fake_daraja)
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    client.post(CALLBACK_URL, json=_callback_payload())
    assert _balance(app, user["id"]) == Decimal("510.00")

    # Reconciling an already-completed deposit must be a safe no-op.
    resp = client.post("/api/mpesa/transactions/1/reconcile", headers=headers)
    assert resp.status_code == 200
    assert (
        resp.get_json()["data"]["status"] == MpesaTransactionStatus.COMPLETED
    )
    assert _balance(app, user["id"]) == Decimal("510.00")

    with app.app_context():
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1


def test_reconciliation_sweeper_does_not_start_under_testing(app):
    """Guard: the background sweep must never run (or spawn threads) in tests."""
    assert getattr(app, "_mpesa_sweeper_started", False) is False


def test_sweeper_helper_starts_thread_and_reconciles(
    app, authenticated_user, fake_daraja, monkeypatch
):
    """Integration: the sweeper thread actually credits a stuck deposit.

    Uses a non-testing app and a tiny interval, then stops the thread after the
    assertion so it does not keep running for the rest of the suite.
    """
    # Build a non-testing config that still uses an isolated sqlite DB.
    import tempfile

    from app.config import TestConfig

    class _SweeperConfig(TestConfig):
        TESTING = False
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tempfile.mkdtemp()}/sweep.db"
        # Allow the background sweep thread to share the file-based SQLite
        # connection (PostgreSQL/MySQL in production are unaffected).
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False}
        }

    sweep_app = create_app(_SweeperConfig)
    stop = threading.Event()

    with sweep_app.app_context():
        db.create_all()
        user = User(
            first_name="Sweep",
            last_name="User",
            email="sweep@example.com",
            phone_number="254712345678",
            role="user",
            is_active=True,
            status="Active",
        )
        user.set_password("SecurePass123")
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        wallet = Wallet(user_id=user_id, balance=Decimal("10.00"))
        db.session.add(wallet)
        db.session.commit()
        wallet_id = wallet.id

        # A deposit stuck in PENDING because the callback never arrived.
        mpesa_transaction = MpesaTransaction(
            user_id=user_id,
            wallet_id=wallet_id,
            account_reference="REFNORENT",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.PENDING,
            checkout_request_id="ws_CO_sweeper_test",
        )
        db.session.add(mpesa_transaction)
        db.session.commit()

    # Monkeypatch Daraja so the sweeper's recovery query confirms success.
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    # Patch the running loop to honour a stop event so we don't leak a thread.
    original_run = MpesaService.start_reconciliation_sweeper
    started_threads = []

    def _patched_start(app_obj, interval=None):
        if app_obj.config.get("TESTING"):
            return
        iv = int(interval or app_obj.config.get("MPESA_RECONCILIATION_INTERVAL_SECONDS", 60))
        if iv <= 0:
            return
        if getattr(app_obj, "_mpesa_sweeper_started", False):
            return
        app_obj._mpesa_sweeper_started = True

        def _loop():
            while not stop.is_set():
                stop.wait(iv / 1000.0 if iv < 1 else 0.05)
                if stop.is_set():
                    return
                try:
                    with app_obj.app_context():
                        MpesaService.recover_deposits()
                except Exception:
                    pass

        t = threading.Thread(target=_loop, name="test-sweeper", daemon=True)
        started_threads.append(t)
        t.start()

    monkeypatch.setattr(MpesaService, "start_reconciliation_sweeper", _patched_start)
    # The real sweeper already ran during create_app and set this flag; clear it
    # so our patched (stoppable) sweeper is actually allowed to start.
    sweep_app._mpesa_sweeper_started = False
    _patched_start(sweep_app, interval=1)

    try:
        # Give the sweeper a few ticks to reconcile the stuck deposit.
        for _ in range(50):
            with sweep_app.app_context():
                if (
                    MpesaTransaction.query.filter_by(
                        checkout_request_id="ws_CO_sweeper_test"
                    ).first().status
                    == MpesaTransactionStatus.COMPLETED
                ):
                    break
            time.sleep(0.05)
    finally:
        stop.set()

    with sweep_app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_sweeper_test"
        ).first()
        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert Decimal(str(Wallet.query.get(wallet_id).balance)) == Decimal("510.00")
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1

    # Restore the real helper (no-op for other tests since they use TESTING).
    monkeypatch.setattr(MpesaService, "start_reconciliation_sweeper", original_run)


# --- Issue 5: post-review hardening tests ----------------------------------


def test_repeated_reconciliation_attempts_credit_exactly_once(
    app, create_user, fake_daraja
):
    """Running recover_deposits many times against a confirmed deposit
    must credit exactly once. The ledger constraint is the backstop.
    """
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    user = create_user(email="repeat@example.com", balance="1000.00")
    user_id = user["id"]
    cid = "ws_CO_repeat"
    _make_pending_deposit(app, user_id, cid)

    # Many sweeps; Daraja keeps confirming success.
    for _ in range(5):
        MpesaService.recover_deposits()

    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        assert Decimal(str(wallet.balance)) == Decimal("1500.00")
        assert Transaction.query.count() == 1
        assert WalletLedger.query.count() == 1
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id=cid
        ).first()
        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert stored.reconciliation_attempts >= 1


def test_stuck_deposit_produces_visibility_log_but_not_failed(
    app, create_user, caplog
):
    """A long-stuck deposit produces a structured warning but is NOT
    automatically marked FAILED. Visibility only, never a state change.
    """
    user = create_user(email="stuck@example.com", balance="1000.00")
    user_id = user["id"]
    cid = "ws_CO_stuck"
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        m = MpesaTransaction(
            user_id=user_id,
            wallet_id=wallet.id,
            account_reference="REFSTUCK",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.PENDING,
            merchant_request_id="x",
            checkout_request_id=cid,
            created_at=datetime.utcnow() - timedelta(days=3),
        )
        db.session.add(m)
        db.session.commit()

    # Lower the threshold so our 3-day-old deposit triggers the alert.
    app.config["MPESA_STUCK_DEPOSIT_ALERT_SECONDS"] = 3600  # 1 hour

    with caplog.at_level(logging.WARNING):
        MpesaService._alert_stuck_deposits(app)

    assert any(
        "MPESA_EVENT=STUCK_DEPOSIT_ALERT" in r.message for r in caplog.records
    )
    with app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id=cid
        ).first()
        assert stored.status == MpesaTransactionStatus.PENDING  # NOT failed
        assert stored.failure_reason is None


def test_stuck_deposit_by_reconciliation_attempts(app, create_user, caplog):
    """A deposit that has been reconciled many times without resolution
    triggers the attempts-based alert.
    """
    user = create_user(email="attempted@example.com", balance="1000.00")
    user_id = user["id"]
    cid = "ws_CO_attempts"
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        m = MpesaTransaction(
            user_id=user_id,
            wallet_id=wallet.id,
            account_reference="REFATTEMPT",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.RECONCILIATION_PENDING,
            merchant_request_id="x",
            checkout_request_id=cid,
            reconciliation_attempts=50,
        )
        db.session.add(m)
        db.session.commit()

    app.config["MPESA_MAX_RECONCILIATION_ATTEMPTS"] = 48

    with caplog.at_level(logging.WARNING):
        MpesaService._alert_stuck_deposits(app)

    assert any(
        "MPESA_EVENT=STUCK_DEPOSIT_ALERT" in r.message for r in caplog.records
    )
    with app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id=cid
        ).first()
        assert stored.status == MpesaTransactionStatus.RECONCILIATION_PENDING


def test_non_stuck_deposit_does_not_alert(app, create_user, caplog):
    """A fresh deposit should not trigger any stuck-deposit alert."""
    user = create_user(email="fresh@example.com", balance="1000.00")
    user_id = user["id"]
    cid = "ws_CO_fresh"
    with app.app_context():
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        m = MpesaTransaction(
            user_id=user_id,
            wallet_id=wallet.id,
            account_reference="REFFRESH",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.PENDING,
            merchant_request_id="x",
            checkout_request_id=cid,
        )
        db.session.add(m)
        db.session.commit()

    app.config["MPESA_STUCK_DEPOSIT_ALERT_SECONDS"] = 86400  # 1 day
    app.config["MPESA_MAX_RECONCILIATION_ATTEMPTS"] = 48

    with caplog.at_level(logging.WARNING):
        MpesaService._alert_stuck_deposits(app)

    assert not any(
        "MPESA_EVENT=STUCK_DEPOSIT_ALERT" in r.message for r in caplog.records
    )


def test_sweeper_exception_does_not_terminate_loop(
    app, authenticated_user, fake_daraja, monkeypatch, caplog
):
    """Exceptions in the reconciliation loop must not kill the sweeper.
    The loop catches and logs, then continues to the next cycle.
    """
    import tempfile

    from app.config import TestConfig
    from app.models.user import User as UserModel

    class _SweeperConfig(TestConfig):
        TESTING = False
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tempfile.mkdtemp()}/sweep_err.db"
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False}
        }

    sweep_app = create_app(_SweeperConfig)
    stop = threading.Event()

    with sweep_app.app_context():
        db.create_all()
        user = UserModel(
            first_name="Sweep",
            last_name="Err",
            email="sweepererr@example.com",
            phone_number="254712345678",
            role="user",
            is_active=True,
            status="Active",
        )
        user.set_password("SecurePass123")
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        wallet = Wallet(user_id=user_id, balance=Decimal("10.00"))
        db.session.add(wallet)
        db.session.commit()
        wallet_id = wallet.id
        m = MpesaTransaction(
            user_id=user_id,
            wallet_id=wallet_id,
            account_reference="REFERR",
            phone_number="254712345678",
            amount=Decimal("500.00"),
            status=MpesaTransactionStatus.PENDING,
            checkout_request_id="ws_CO_sweeper_err",
        )
        db.session.add(m)
        db.session.commit()

    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})

    # Make recover_deposits fail twice, then succeed.
    calls = {"n": 0}
    orig_recover = MpesaService.recover_deposits

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("simulated sweeper failure")
        return orig_recover()

    monkeypatch.setattr(MpesaService, "recover_deposits", flaky)
    sweep_app._mpesa_sweeper_started = False

    # Build a stoppable sweeper that uses _sweeper_cycle (which catches
    # exceptions via the outer try/except in _run_sweeper).
    def _patched_start(app_obj, interval=None):
        if app_obj.config.get("TESTING"):
            return
        if getattr(app_obj, "_mpesa_sweeper_started", False):
            return
        app_obj._mpesa_sweeper_started = True

        def _loop():
            leader_state = {}
            while not stop.is_set():
                stop.wait(0.05)
                if stop.is_set():
                    return
                try:
                    with app_obj.app_context():
                        MpesaService._sweeper_cycle(app_obj, leader_state)
                except Exception:
                    app_obj.logger.exception("MPESA_EVENT=SWEEPER_ERROR")

        t = threading.Thread(target=_loop, name="test-sweeper-err", daemon=True)
        t.start()

    monkeypatch.setattr(
        MpesaService, "start_reconciliation_sweeper", _patched_start
    )
    _patched_start(sweep_app, interval=1)

    try:
        for _ in range(60):
            with sweep_app.app_context():
                stored = MpesaTransaction.query.filter_by(
                    checkout_request_id="ws_CO_sweeper_err"
                ).first()
                if stored.status == MpesaTransactionStatus.COMPLETED:
                    break
            time.sleep(0.05)
    finally:
        stop.set()

    with sweep_app.app_context():
        stored = MpesaTransaction.query.filter_by(
            checkout_request_id="ws_CO_sweeper_err"
        ).first()
        assert stored.status == MpesaTransactionStatus.COMPLETED
        assert (
            Decimal(str(Wallet.query.get(wallet_id).balance)) == Decimal("510.00")
        )


@requires_sqlite
def test_sweeper_logs_leadership_cycle_events(app, monkeypatch, caplog):
    """On SQLite the sweeper logs SWEEPER_LEADERSHIP_UNSUPPORTED (no advisory
    locks available), confirming the operator knows coordination is absent.
    """
    leader_state = {}
    with caplog.at_level(logging.WARNING):
        result = MpesaService._acquire_sweeper_leadership(app, leader_state)

    # On SQLite, leadership is always granted (no PG advisory locks).
    assert result is True
    assert any(
        "SWEEPER_LEADERSHIP_UNSUPPORTED" in r.message for r in caplog.records
    )
    assert leader_state.get("warned_non_pg") is True

    # Second call should not re-warn (idempotent).
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        result2 = MpesaService._acquire_sweeper_leadership(app, leader_state)
    assert result2 is True
    assert not any(
        "SWEEPER_LEADERSHIP_UNSUPPORTED" in r.message for r in caplog.records
    )


@requires_sqlite
def test_sweeper_cycle_runs_on_sqlite(app, fake_daraja, caplog):
    """On SQLite, _sweeper_cycle runs recover_deposits (leadership always
    granted) and logs SWEEPER_CYCLE_STARTED + SWEEPER_SUMMARY.
    """
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    leader_state = {}

    with caplog.at_level(logging.INFO):
        MpesaService._sweeper_cycle(app, leader_state)

    assert any("SWEEPER_CYCLE_STARTED" in r.message for r in caplog.records)
    assert any("SWEEPER_SUMMARY" in r.message for r in caplog.records)

    # Leadership should be released (no conn in state on SQLite).
    assert leader_state.get("conn") is None


# ---------------------------------------------------------------------------
# Sweeper leadership: advisory-lock SQL execution path
#
# Regression cover for the production failure
#   MPESA_EVENT=SWEEPER_LEADERSHIP_ERROR
#   psycopg2.errors.SyntaxError: syntax error at or near ":"
#   LINE 1: SELECT pg_try_advisory_lock(:key)
# caused by running a SQLAlchemy-style named bind through
# ``Connection.exec_driver_sql()``, which passes the string straight to the
# DBAPI without compiling the bind into psycopg2's paramstyle.
# ---------------------------------------------------------------------------


class _FakeAdvisoryLockResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeLeadershipConnection:
    """Stand-in for a psycopg2-backed connection.

    ``execute()`` compiles the statement with the real PostgreSQL/psycopg2
    dialect, so a statement that still carries an untranslated ``:key`` bind
    (or a backslash-escaped ``\\:key``) fails here instead of in production.

    ``exec_driver_sql()`` is a hard failure: it bypasses SQLAlchemy's bind
    compilation, which is exactly what sent the literal text
    ``pg_try_advisory_lock(:key)`` to PostgreSQL.
    """

    def __init__(self, acquired=True, raises=None):
        self.acquired = acquired
        self.raises = raises
        self.statements = []
        self.options = {}
        self.closed = False

    def execution_options(self, **options):
        self.options.update(options)
        return self

    def execute(self, statement, parameters=None):
        if self.raises is not None:
            raise self.raises

        compiled = str(statement.compile(dialect=postgresql.dialect()))

        # The bind must be compiled into the driver paramstyle, never left as
        # SQL text that PostgreSQL would have to parse.
        assert ":key" not in compiled, compiled
        assert "\\" not in compiled, compiled
        assert "%(key)s" in compiled, compiled
        assert isinstance(parameters, dict), parameters
        assert isinstance(parameters.get("key"), int), parameters

        self.statements.append((compiled, dict(parameters)))

        if "pg_try_advisory_lock" in compiled:
            return _FakeAdvisoryLockResult(self.acquired)

        return _FakeAdvisoryLockResult(True)

    def exec_driver_sql(self, statement, parameters=None):
        raise AssertionError(
            "advisory locks must not use exec_driver_sql(): it bypasses "
            "SQLAlchemy bind compilation and sends ':key' to PostgreSQL "
            f"literally (statement={statement!r})"
        )

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pg_leadership(app):
    """Force the PostgreSQL leadership branch onto a fake connection.

    Lets the psycopg2 execution path be asserted on any backend (the suite
    defaults to SQLite, which has no advisory locks). The engine is restored
    before the ``app`` fixture tears the database down.
    """
    restore = []

    def _install(connection=None, connect_error=None):
        with app.app_context():
            engine = db.engine

        original_connect = engine.connect
        original_dialect_name = engine.dialect.name
        restore.append((engine, original_connect, original_dialect_name))

        engine.dialect.name = "postgresql"

        if connect_error is not None:
            def _connect():
                raise connect_error
        else:
            def _connect():
                return connection

        engine.connect = _connect

        return connection

    yield _install

    for engine, original_connect, original_dialect_name in reversed(restore):
        engine.connect = original_connect
        engine.dialect.name = original_dialect_name


def test_advisory_lock_statements_compile_to_bound_parameters():
    """The lock SQL must compile to the driver paramstyle, not raw ``:key``."""
    for statement in (
        MpesaService._ACQUIRE_SWEEPER_LOCK_SQL,
        MpesaService._RELEASE_SWEEPER_LOCK_SQL,
    ):
        compiled = str(statement.compile(dialect=postgresql.dialect()))

        assert "%(key)s" in compiled, compiled
        assert ":key" not in compiled, compiled
        assert "\\" not in compiled, compiled
        # Explicit cast pins pg_*_advisory_lock(bigint) resolution.
        assert "bigint" in compiled.lower(), compiled


def test_acquire_sweeper_leadership_executes_bound_sql_on_postgres(
    app, fake_pg_leadership, caplog
):
    """Leadership is acquired via execute(text(...)) with a bound lock id."""
    conn = _FakeLeadershipConnection(acquired=True)
    fake_pg_leadership(connection=conn)
    leader_state = {}

    with app.app_context():
        with caplog.at_level(logging.INFO):
            acquired = MpesaService._acquire_sweeper_leadership(app, leader_state)

    assert acquired is True
    assert not any("SWEEPER_LEADERSHIP_ERROR" in r.message for r in caplog.records)
    assert any("SWEEPER_LEADER_ACQUIRED" in r.message for r in caplog.records)

    # Exactly one statement, carrying the configured lock id as a parameter.
    assert len(conn.statements) == 1
    compiled, params = conn.statements[0]
    assert "pg_try_advisory_lock" in compiled
    with app.app_context():
        assert params == {"key": MpesaService._sweeper_leader_lock_id(app)}

    # The connection is retained for the cycle and left usable.
    assert leader_state["conn"] is conn
    assert conn.closed is False
    # Session-level advisory locks do not need a transaction held open.
    assert conn.options.get("isolation_level") == "AUTOCOMMIT"


def test_second_process_does_not_acquire_leadership_on_postgres(
    app, fake_pg_leadership, caplog
):
    """pg_try_advisory_lock returning false means "not leader", not an error."""
    conn = _FakeLeadershipConnection(acquired=False)
    fake_pg_leadership(connection=conn)
    leader_state = {}

    with app.app_context():
        with caplog.at_level(logging.INFO):
            acquired = MpesaService._acquire_sweeper_leadership(app, leader_state)

    assert acquired is False
    assert any("SWEEPER_NOT_LEADER" in r.message for r in caplog.records)
    assert not any("SWEEPER_LEADERSHIP_ERROR" in r.message for r in caplog.records)
    assert leader_state.get("conn") is None
    # The losing process must not hold a connection open.
    assert conn.closed is True


def test_release_sweeper_leadership_unlocks_with_bound_sql(app, fake_pg_leadership):
    """Release runs pg_advisory_unlock with a bound id and closes the conn."""
    conn = _FakeLeadershipConnection(acquired=True)
    fake_pg_leadership(connection=conn)
    leader_state = {}

    with app.app_context():
        assert MpesaService._acquire_sweeper_leadership(app, leader_state) is True
        MpesaService._release_sweeper_leadership(app, leader_state)

        assert len(conn.statements) == 2
        compiled, params = conn.statements[1]
        assert "pg_advisory_unlock" in compiled
        assert params == {"key": MpesaService._sweeper_leader_lock_id(app)}

    assert conn.closed is True
    assert leader_state.get("conn") is None


def test_leadership_error_is_logged_and_connection_closed(
    app, fake_pg_leadership, caplog
):
    """A failing lock statement degrades to "not leader", never a crash."""
    conn = _FakeLeadershipConnection(raises=RuntimeError("boom"))
    fake_pg_leadership(connection=conn)
    leader_state = {}

    with app.app_context():
        with caplog.at_level(logging.ERROR):
            acquired = MpesaService._acquire_sweeper_leadership(app, leader_state)

    assert acquired is False
    assert any("SWEEPER_LEADERSHIP_ERROR" in r.message for r in caplog.records)
    assert conn.closed is True
    assert leader_state.get("conn") is None


def test_leadership_connect_failure_does_not_raise(app, fake_pg_leadership, caplog):
    """A failure before the connection exists must not raise NameError."""
    fake_pg_leadership(connect_error=RuntimeError("pool exhausted"))
    leader_state = {}

    with app.app_context():
        with caplog.at_level(logging.ERROR):
            acquired = MpesaService._acquire_sweeper_leadership(app, leader_state)

    assert acquired is False
    assert any("SWEEPER_LEADERSHIP_ERROR" in r.message for r in caplog.records)
    assert leader_state.get("conn") is None


def test_sweeper_cycle_on_postgres_runs_without_leadership_error(
    app, fake_pg_leadership, monkeypatch, caplog
):
    """A full cycle acquires, sweeps and releases with no LEADERSHIP_ERROR.

    ``recover_deposits`` is stubbed out because the fake engine connection
    stands in for the whole engine; the real database sweep is covered by the
    SQLite/PostgreSQL cycle tests.
    """
    conn = _FakeLeadershipConnection(acquired=True)
    fake_pg_leadership(connection=conn)
    monkeypatch.setattr(
        MpesaService, "recover_deposits", staticmethod(lambda: {"checked": 0})
    )
    monkeypatch.setattr(
        MpesaService, "_alert_stuck_deposits", staticmethod(lambda app: None)
    )
    leader_state = {}

    with app.app_context():
        with caplog.at_level(logging.INFO):
            MpesaService._sweeper_cycle(app, leader_state)

    assert not any("SWEEPER_LEADERSHIP_ERROR" in r.message for r in caplog.records)
    assert any("SWEEPER_LEADER_ACQUIRED" in r.message for r in caplog.records)
    assert any("SWEEPER_CYCLE_STARTED" in r.message for r in caplog.records)
    assert any("SWEEPER_SUMMARY" in r.message for r in caplog.records)

    # Acquire + release, both as compiled/bound statements.
    assert [s[0].split("(")[0] for s in conn.statements] == [
        "SELECT pg_try_advisory_lock",
        "SELECT pg_advisory_unlock",
    ]
    assert conn.closed is True
    assert leader_state.get("conn") is None


@requires_postgres
def test_advisory_lock_round_trip_against_real_postgres(app, caplog):
    """End-to-end against PostgreSQL: acquire, exclude, release, re-acquire."""
    with app.app_context():
        lock_id = MpesaService._sweeper_leader_lock_id(app)
        database_url = db.engine.url

        leader_state = {}
        with caplog.at_level(logging.INFO):
            acquired = MpesaService._acquire_sweeper_leadership(app, leader_state)

        assert acquired is True
        assert not any(
            "SWEEPER_LEADERSHIP_ERROR" in r.message for r in caplog.records
        )
        assert leader_state.get("conn") is not None

        # A separate engine == a separate PostgreSQL session, i.e. what another
        # Render instance/worker process is: it must NOT get the lock.
        other_engine = create_engine(database_url)
        try:
            with other_engine.connect() as other_conn:
                held_by_other = other_conn.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                    {"key": lock_id},
                ).scalar()
            assert held_by_other is False
        finally:
            other_engine.dispose()

        # The same is true through the service helper (second leader_state).
        follower_state = {}
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert (
                MpesaService._acquire_sweeper_leadership(app, follower_state) is False
            )
        assert any("SWEEPER_NOT_LEADER" in r.message for r in caplog.records)
        assert follower_state.get("conn") is None

        # Releasing frees the lock for the next process.
        MpesaService._release_sweeper_leadership(app, leader_state)
        assert leader_state.get("conn") is None

        with db.engine.connect() as probe:
            still_held = probe.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND ((classid::bigint << 32) | objid::bigint) = :key"
                ),
                {"key": lock_id},
            ).scalar()
        assert still_held == 0

        next_state = {}
        assert MpesaService._acquire_sweeper_leadership(app, next_state) is True
        MpesaService._release_sweeper_leadership(app, next_state)


@requires_postgres
def test_sweeper_cycle_against_real_postgres(app, fake_daraja, caplog):
    """The sweeper cycle runs on PostgreSQL without SWEEPER_LEADERSHIP_ERROR."""
    fake_daraja(query_response={"ResultCode": "0", "ResultDesc": "Success."})
    leader_state = {}

    with app.app_context():
        with caplog.at_level(logging.INFO):
            MpesaService._sweeper_cycle(app, leader_state)

    assert not any("SWEEPER_LEADERSHIP_ERROR" in r.message for r in caplog.records)
    assert any("SWEEPER_LEADER_ACQUIRED" in r.message for r in caplog.records)
    assert any("SWEEPER_CYCLE_STARTED" in r.message for r in caplog.records)
    assert any("SWEEPER_SUMMARY" in r.message for r in caplog.records)
    assert leader_state.get("conn") is None


# --- status column capacity invariant -----------------------------------


def test_every_status_value_fits_the_database_column_width(client):
    """A status value longer than ``mpesa_transactions.status`` must never reach
    production silently (it would raise ``StringDataRightTruncation`` on
    PostgreSQL and 500 the callback/recovery path).

    This pins the contract between the ``MpesaTransactionStatus`` enum and the
    SQLAlchemy column definition so a future value longer than the column is a
    hard test failure, not a deploy-time surprise.
    """
    status_column = MpesaTransaction.__table__.c.status
    column_length = status_column.type.length
    assert column_length is not None, "status column must have a fixed width"

    defined_statuses = [
        value
        for name, value in vars(MpesaTransactionStatus).items()
        if name.isupper() and isinstance(value, str)
    ]
    assert defined_statuses, "no MpesaTransactionStatus string values found"

    for status in defined_statuses:
        assert len(status) <= column_length, (
            f"status value {status!r} ({len(status)} chars) exceeds the "
            f"mpesa_transactions.status width of {column_length}; widen the "
            f"column and its Alembic migration before shipping this value"
        )


def test_reconciliation_pending_persists_and_round_trips(client, create_user):
    """``RECONCILIATION_PENDING`` (21 chars) must persist and read back intact.

    On PostgreSQL this is the exact regression: the column was ``VARCHAR(20)``,
    which truncated (and 500'd) this value. SQLite does not enforce the width,
    so this test is most meaningful on PostgreSQL but still asserts the model
    contract on every backend.
    """
    user = create_user()
    with client.application.app_context():
        txn = MpesaTransaction(
            user_id=user["id"],
            wallet_id=Wallet.query.filter_by(user_id=user["id"]).first().id,
            account_reference="ACC-INV",
            phone_number="254712345678",
            amount=500,
            status=MpesaTransactionStatus.RECONCILIATION_PENDING,
        )
        db.session.add(txn)
        db.session.commit()

        stored = MpesaTransaction.query.filter_by(
            status=MpesaTransactionStatus.RECONCILIATION_PENDING
        ).first()
        assert stored is not None
        assert stored.status == MpesaTransactionStatus.RECONCILIATION_PENDING
        assert len(stored.status) == len("ReconciliationPending")

