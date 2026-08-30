"""Tests for the simulated service payment feature.

Covers:
- Provider logic (deterministic scenarios, validation, service-specific metadata)
- API endpoints (auth, CRUD, reconciliation)
- Financial integrity (wallet debits, refunds, ledger entries, idempotency)
- Regression safety (existing features not broken)
"""

from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    LedgerEntryType,
    ServicePayment,
    ServicePaymentStatus,
    ServiceProvider,
    ServiceType,
    Transaction,
    TransactionType,
    Wallet,
    WalletLedger,
)
from app.services.providers import resolve_provider
from app.services.providers.airtime import AirtimeProvider
from app.services.providers.base import (
    SCENARIO_FAILED_PREFIXES,
    SCENARIO_PENDING_PREFIXES,
    SCENARIO_SUCCESS_PREFIXES,
    BaseProvider,
    ProviderResult,
)
from app.services.providers.electricity import ElectricityProvider
from app.services.providers.water import WaterProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SUCCESS_ACCOUNT = "1111111111"
PENDING_ACCOUNT = "2222222222"
FAILED_ACCOUNT = "3333333333"
NORMAL_ACCOUNT = "1234567890"


@pytest.fixture(autouse=True)
def seed_service_providers(app):
    """Seed the service_providers table so API tests can list services."""
    with app.app_context():
        for stype, name, display, desc in [
            ("ELECTRICITY", "Electricity", "Electricity", "Purchase simulated prepaid electricity"),
            ("WATER", "Water", "Water", "Pay a simulated water bill"),
            ("AIRTIME", "Airtime", "Airtime", "Purchase simulated airtime"),
        ]:
            if not ServiceProvider.query.filter_by(service_type=stype).first():
                db.session.add(
                    ServiceProvider(
                        name=name,
                        service_type=stype,
                        display_name=display,
                        description=desc,
                        is_active=True,
                    )
                )
        db.session.commit()


# ===================================================================
# SECTION 1: Provider Unit Tests
# ===================================================================


class TestProviderResolution:
    """Test the provider resolver."""

    def test_resolve_electricity(self):
        assert resolve_provider("ELECTRICITY") is ElectricityProvider

    def test_resolve_water(self):
        assert resolve_provider("WATER") is WaterProvider

    def test_resolve_airtime(self):
        assert resolve_provider("AIRTIME") is AirtimeProvider

    def test_resolve_unknown_raises(self):
        from app.utils.errors import ApiError, ErrorCode

        with pytest.raises(ApiError) as exc_info:
            resolve_provider("GAS")
        assert exc_info.value.error_code == ErrorCode.INVALID_SERVICE_TYPE


class TestDeterministicScenarios:
    """Test that payment outcomes are deterministic based on account number."""

    def test_success_scenario_electricity(self):
        result = ElectricityProvider.process(SUCCESS_ACCOUNT, Decimal("500.00"), "REF-001")
        assert result.status == "COMPLETED"
        assert "token" in result.metadata
        assert "units" in result.metadata

    def test_pending_scenario_electricity(self):
        result = ElectricityProvider.process(PENDING_ACCOUNT, Decimal("500.00"), "REF-002")
        assert result.status == "PENDING"
        assert "pending_reason" in result.metadata

    def test_failed_scenario_electricity(self):
        result = ElectricityProvider.process(FAILED_ACCOUNT, Decimal("500.00"), "REF-003")
        assert result.status == "FAILED"
        assert result.failure_reason is not None

    def test_success_scenario_water(self):
        result = WaterProvider.process(SUCCESS_ACCOUNT, Decimal("800.00"), "REF-004")
        assert result.status == "COMPLETED"
        assert "receipt_number" in result.metadata
        # Water should NOT have electricity token
        assert "token" not in result.metadata

    def test_success_scenario_airtime(self):
        result = AirtimeProvider.process("254111111111", Decimal("100.00"), "REF-005")
        assert result.status == "COMPLETED"
        assert "confirmation_reference" in result.metadata

    def test_normal_account_defaults_to_success(self):
        result = ElectricityProvider.process(NORMAL_ACCOUNT, Decimal("500.00"), "REF-006")
        assert result.status == "COMPLETED"


class TestElectricityProvider:
    """Test electricity-specific behavior."""

    def test_token_format(self):
        result = ElectricityProvider.process(SUCCESS_ACCOUNT, Decimal("500.00"), "REF-010")
        token = result.metadata["token"]
        # Format: XXXX-XXXX-XXXX-XXXX-XXXX
        parts = token.split("-")
        assert len(parts) == 5
        for part in parts:
            assert len(part) == 4
            assert part.isdigit()

    def test_units_calculation(self):
        result = ElectricityProvider.process(SUCCESS_ACCOUNT, Decimal("1000.00"), "REF-011")
        units = result.metadata["units"]
        # 1000 * 0.0136 = 13.6
        assert units == pytest.approx(13.6, abs=0.01)

    def test_meter_number_masked(self):
        result = ElectricityProvider.process("1234567890", Decimal("500.00"), "REF-012")
        masked = result.metadata["meter_number_masked"]
        assert masked == "******7890"

    def test_validate_requires_digits_only(self):
        from app.utils.errors import ApiError

        with pytest.raises(ApiError):
            ElectricityProvider.validate("abcdef1234", Decimal("500.00"))

    def test_validate_rejects_too_short(self):
        from app.utils.errors import ApiError

        with pytest.raises(ApiError):
            ElectricityProvider.validate("12345", Decimal("500.00"))

    def test_validate_rejects_too_long(self):
        from app.utils.errors import ApiError

        with pytest.raises(ApiError):
            ElectricityProvider.validate("1" * 20, Decimal("500.00"))

    def test_validate_rejects_zero_amount(self):
        from app.utils.errors import ApiError

        with pytest.raises(ApiError):
            ElectricityProvider.validate("1234567890", Decimal("0.00"))

    def test_validate_rejects_negative_amount(self):
        from app.utils.errors import ApiError

        with pytest.raises(ApiError):
            ElectricityProvider.validate("1234567890", Decimal("-100.00"))


class TestWaterProvider:
    """Test water-specific behavior."""

    def test_receipt_number_format(self):
        result = WaterProvider.process(SUCCESS_ACCOUNT, Decimal("500.00"), "REF-020")
        receipt = result.metadata["receipt_number"]
        assert receipt.startswith("VYL-WTR-")

    def test_account_masked(self):
        result = WaterProvider.process("1234567890", Decimal("500.00"), "REF-021")
        masked = result.metadata["account_number_masked"]
        assert masked == "******7890"


class TestAirtimeProvider:
    """Test airtime-specific behavior."""

    def test_confirmation_reference_format(self):
        result = AirtimeProvider.process("254111111111", Decimal("100.00"), "REF-030")
        confirmation = result.metadata["confirmation_reference"]
        assert confirmation.startswith("VYL-ATM-")

    def test_phone_number_masked(self):
        result = AirtimeProvider.process("254712345678", Decimal("100.00"), "REF-031")
        masked = result.metadata["phone_number_masked"]
        assert masked.endswith("5678")

    def test_validate_normalizes_phone(self):
        # 0712345678 -> 254712345678
        cleaned = AirtimeProvider.validate("0712345678", Decimal("100.00"))
        assert cleaned == "254712345678"

    def test_validate_rejects_invalid_phone(self):
        from app.utils.errors import ApiError

        with pytest.raises(ApiError):
            AirtimeProvider.validate("12345", Decimal("100.00"))

    def test_validate_rejects_non_string(self):
        from app.utils.errors import ApiError

        with pytest.raises(ApiError):
            AirtimeProvider.validate(None, Decimal("100.00"))


# ===================================================================
# SECTION 2: API Endpoint Tests
# ===================================================================


SERVICES_URL = "/api/services"
PAYMENTS_URL = "/api/service-payments"


class TestListServicesEndpoint:
    """GET /api/services"""

    def test_requires_authentication(self, client):
        response = client.get(SERVICES_URL)
        assert response.status_code == 401

    def test_returns_available_services(self, client, authenticated_user, seed_service_providers):
        _, headers = authenticated_user(email="svc@example.com", balance="1000.00")
        response = client.get(SERVICES_URL, headers=headers)

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        services = data["data"]["services"]
        assert len(services) == 3

        types = {s["type"] for s in services}
        assert types == {"ELECTRICITY", "WATER", "AIRTIME"}

    def test_listing_exposes_canonical_service_type_key(
        self, client, authenticated_user, seed_service_providers
    ):
        """Every provider must expose ``service_type`` (the key the POST
        contract uses), with ``type`` kept as an identical legacy alias.

        Regression guard: the listing previously exposed only ``type``, so a
        client reading ``service_type`` got ``undefined``/empty and the payment
        POST was rejected with 400 INVALID_SERVICE_TYPE.
        """
        _, headers = authenticated_user(email="svckey@example.com", balance="1000.00")
        response = client.get(SERVICES_URL, headers=headers)

        assert response.status_code == 200
        services = response.get_json()["data"]["services"]

        for service in services:
            assert "service_type" in service
            assert service["service_type"] in ServiceType.ALL
            # Legacy alias must stay in sync with the canonical key.
            assert service["type"] == service["service_type"]

        assert {s["service_type"] for s in services} == set(ServiceType.ALL)


class TestServiceTypeContract:
    """The GET /api/services value must be directly usable in the POST body.

    These tests pin the exact frontend/backend contract: the value returned by
    ``GET /api/services`` under ``service_type`` is posted verbatim as
    ``service_type`` to ``POST /api/service-payments``.
    """

    def test_listed_service_type_is_accepted_verbatim_by_payment_endpoint(
        self, client, authenticated_user, seed_service_providers
    ):
        user, headers = authenticated_user(email="contract@example.com", balance="3000.00")

        listing = client.get(SERVICES_URL, headers=headers)
        assert listing.status_code == 200
        services = listing.get_json()["data"]["services"]
        assert len(services) == 3

        for service in services:
            service_type = service["service_type"]
            account_number = (
                "254111111111" if service_type == "AIRTIME" else SUCCESS_ACCOUNT
            )

            response = client.post(
                PAYMENTS_URL,
                json={
                    "service_type": service_type,
                    "account_number": account_number,
                    "amount": 100,
                },
                headers=headers,
            )

            assert response.status_code == 201, response.get_json()
            payment = response.get_json()["data"]["payment"]
            assert payment["service_type"] == service_type
            assert payment["status"] == ServicePaymentStatus.COMPLETED

    def test_empty_service_type_rejected(self, client, authenticated_user):
        """The exact payload the broken frontend sent (empty service_type)."""
        _, headers = authenticated_user(email="emptytype@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["error"] == "INVALID_SERVICE_TYPE"
        assert body["message"] == (
            "Invalid service type. Must be one of: ELECTRICITY, WATER, AIRTIME"
        )

    def test_null_service_type_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="nulltype@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": None,
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "INVALID_SERVICE_TYPE"

    def test_display_label_is_rejected(self, client, authenticated_user):
        """A display label (e.g. "Electricity Bill") is not an enum value."""
        _, headers = authenticated_user(email="labeltype@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "Electricity Bill",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "INVALID_SERVICE_TYPE"

    @pytest.mark.parametrize(
        "sent, expected",
        [
            ("electricity", "ELECTRICITY"),
            ("water", "WATER"),
            ("airtime", "AIRTIME"),
            ("  Electricity  ", "ELECTRICITY"),
        ],
    )
    def test_case_and_whitespace_are_normalized(
        self, client, authenticated_user, sent, expected
    ):
        """Documented leniency: the backend upper-cases and trims the value.

        Clients should still send the canonical UPPERCASE enum value.
        """
        _, headers = authenticated_user(
            email=f"norm-{expected.lower()}@example.com", balance="1000.00"
        )
        account_number = "254111111111" if expected == "AIRTIME" else SUCCESS_ACCOUNT

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": sent,
                "account_number": account_number,
                "amount": 100,
            },
            headers=headers,
        )

        assert response.status_code == 201, response.get_json()
        assert response.get_json()["data"]["payment"]["service_type"] == expected


class TestCreateServicePaymentEndpoint:
    """POST /api/service-payments"""

    def test_requires_authentication(self, client):
        response = client.post(PAYMENTS_URL, json={})
        assert response.status_code == 401

    def test_successful_electricity_payment(self, client, authenticated_user, wallet_balance):
        user, headers = authenticated_user(email="elec@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 201
        data = response.get_json()
        assert data["success"] is True
        payment = data["data"]["payment"]
        assert payment["service_type"] == "ELECTRICITY"
        assert payment["status"] == "Completed"
        assert payment["result_metadata"] is not None
        assert "token" in payment["result_metadata"]

        # Wallet should be debited
        assert wallet_balance(user["id"]) == Decimal("500.00")

    def test_successful_water_payment(self, client, authenticated_user, wallet_balance):
        user, headers = authenticated_user(email="water@example.com", balance="2000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "WATER",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 800,
            },
            headers=headers,
        )

        assert response.status_code == 201
        payment = response.get_json()["data"]["payment"]
        assert payment["service_type"] == "WATER"
        assert payment["status"] == "Completed"
        assert "receipt_number" in payment["result_metadata"]
        assert wallet_balance(user["id"]) == Decimal("1200.00")

    def test_successful_airtime_payment(self, client, authenticated_user, wallet_balance):
        user, headers = authenticated_user(email="airtime@example.com", balance="500.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "AIRTIME",
                "account_number": "254111111111",
                "amount": 100,
            },
            headers=headers,
        )

        assert response.status_code == 201
        payment = response.get_json()["data"]["payment"]
        assert payment["service_type"] == "AIRTIME"
        assert payment["status"] == "Completed"
        assert "confirmation_reference" in payment["result_metadata"]
        assert wallet_balance(user["id"]) == Decimal("400.00")

    def test_pending_payment(self, client, authenticated_user, wallet_balance):
        user, headers = authenticated_user(email="pending@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": PENDING_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 201
        payment = response.get_json()["data"]["payment"]
        assert payment["status"] == "Pending"

        # Wallet is debited even for pending payments
        assert wallet_balance(user["id"]) == Decimal("500.00")

    def test_failed_payment_refunds_wallet(self, client, authenticated_user, wallet_balance):
        user, headers = authenticated_user(email="fail@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": FAILED_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 201
        payment = response.get_json()["data"]["payment"]
        assert payment["status"] == "Refunded"

        # Wallet should be restored (failed -> refund)
        assert wallet_balance(user["id"]) == Decimal("1000.00")

    def test_insufficient_balance_rejected(self, client, authenticated_user, wallet_balance):
        user, headers = authenticated_user(email="poor@example.com", balance="100.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["error"] == "INSUFFICIENT_BALANCE"
        # Balance unchanged
        assert wallet_balance(user["id"]) == Decimal("100.00")

    def test_invalid_service_type_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="badtype@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "GAS",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "INVALID_SERVICE_TYPE"

    def test_missing_service_type_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="notype@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={"account_number": SUCCESS_ACCOUNT, "amount": 500},
            headers=headers,
        )

        assert response.status_code == 400

    def test_missing_account_number_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="noacct@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={"service_type": "ELECTRICITY", "amount": 500},
            headers=headers,
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "INVALID_ACCOUNT_NUMBER"

    def test_invalid_amount_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="badamt@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": -100,
            },
            headers=headers,
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "INVALID_AMOUNT"

    def test_zero_amount_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="zeroamt@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 0,
            },
            headers=headers,
        )

        assert response.status_code == 400

    def test_account_number_with_letters_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="letters@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": "abc1234567",
                "amount": 500,
            },
            headers=headers,
        )

        assert response.status_code == 400

    def test_unexpected_fields_rejected(self, client, authenticated_user):
        _, headers = authenticated_user(email="extra@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
                "hacker_field": "oops",
            },
            headers=headers,
        )

        assert response.status_code == 400

    def test_account_number_is_masked_in_response(self, client, authenticated_user):
        _, headers = authenticated_user(email="mask@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": "1234567890",
                "amount": 500,
            },
            headers=headers,
        )

        payment = response.get_json()["data"]["payment"]
        assert payment["account_number"] == "****7890"

    def test_payment_creates_transaction_record(self, client, authenticated_user, app):
        _, headers = authenticated_user(email="tx@example.com", balance="1000.00")

        response = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        payment = response.get_json()["data"]["payment"]
        assert payment["transaction_id"] is not None

        with app.app_context():
            tx = db.session.get(Transaction, payment["transaction_id"])
            assert tx is not None
            assert tx.tx_type == TransactionType.SERVICE_PAYMENT
            assert tx.amount == Decimal("500.00")
            assert tx.fee == Decimal("0.00")


class TestListServicePaymentsEndpoint:
    """GET /api/service-payments"""

    def test_requires_authentication(self, client):
        response = client.get(PAYMENTS_URL)
        assert response.status_code == 401

    def test_returns_paginated_payments(self, client, authenticated_user):
        _, headers = authenticated_user(email="list@example.com", balance="10000.00")

        # Create a few payments
        for i in range(3):
            client.post(
                PAYMENTS_URL,
                json={
                    "service_type": "ELECTRICITY",
                    "account_number": SUCCESS_ACCOUNT,
                    "amount": 100,
                },
                headers=headers,
            )

        response = client.get(PAYMENTS_URL, headers=headers)
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        payments = data["data"]["payments"]
        assert len(payments) == 3

        pagination = data["data"]["pagination"]
        assert pagination["total"] == 3
        assert pagination["page"] == 1

    def test_only_returns_own_payments(self, client, authenticated_user):
        """Ownership protection: user A cannot see user B's payments."""
        _, headers_a = authenticated_user(email="owner_a@example.com", balance="10000.00")

        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 100,
            },
            headers=headers_a,
        )

        # Create a second user with no payments
        _, headers_b = authenticated_user(email="owner_b@example.com", balance="1000.00")

        response = client.get(PAYMENTS_URL, headers=headers_b)
        assert response.status_code == 200
        assert len(response.get_json()["data"]["payments"]) == 0

    def test_pagination_params(self, client, authenticated_user):
        _, headers = authenticated_user(email="page@example.com", balance="10000.00")

        for _ in range(5):
            client.post(
                PAYMENTS_URL,
                json={
                    "service_type": "WATER",
                    "account_number": SUCCESS_ACCOUNT,
                    "amount": 50,
                },
                headers=headers,
            )

        response = client.get(f"{PAYMENTS_URL}?page=1&per_page=2", headers=headers)
        data = response.get_json()
        assert len(data["data"]["payments"]) == 2
        assert data["data"]["pagination"]["total"] == 5
        assert data["data"]["pagination"]["pages"] == 3


class TestGetServicePaymentEndpoint:
    """GET /api/service-payments/<id>"""

    def test_requires_authentication(self, client):
        response = client.get(f"{PAYMENTS_URL}/1")
        assert response.status_code == 401

    def test_returns_single_payment(self, client, authenticated_user):
        _, headers = authenticated_user(email="single@example.com", balance="1000.00")

        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment_id = create_resp.get_json()["data"]["payment"]["id"]

        response = client.get(f"{PAYMENTS_URL}/{payment_id}", headers=headers)
        assert response.status_code == 200
        payment = response.get_json()["data"]["payment"]
        assert payment["id"] == payment_id
        assert payment["service_type"] == "ELECTRICITY"

    def test_not_found_for_other_users_payment(self, client, authenticated_user):
        """Ownership: user B cannot access user A's payment by ID."""
        _, headers_a = authenticated_user(email="owner_a2@example.com", balance="1000.00")

        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 100,
            },
            headers=headers_a,
        )
        payment_id = create_resp.get_json()["data"]["payment"]["id"]

        _, headers_b = authenticated_user(email="owner_b2@example.com", balance="1000.00")

        response = client.get(f"{PAYMENTS_URL}/{payment_id}", headers=headers_b)
        assert response.status_code == 404

    def test_404_for_nonexistent_payment(self, client, authenticated_user):
        _, headers = authenticated_user(email="ghost@example.com", balance="1000.00")
        response = client.get(f"{PAYMENTS_URL}/99999", headers=headers)
        assert response.status_code == 404


class TestReconcileServicePaymentEndpoint:
    """POST /api/service-payments/<id>/reconcile"""

    def test_requires_authentication(self, client):
        response = client.post(f"{PAYMENTS_URL}/1/reconcile")
        assert response.status_code == 401

    def test_reconcile_pending_to_completed(self, client, authenticated_user, wallet_balance):
        user, headers = authenticated_user(email="recon1@example.com", balance="1000.00")

        # Create a pending payment
        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": PENDING_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment = create_resp.get_json()["data"]["payment"]
        assert payment["status"] == "Pending"
        payment_id = payment["id"]

        # Reconcile finalizes the pending payment to Completed so funds are not
        # stranded in a perpetual pending state.
        response = client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers)
        assert response.status_code == 200
        reconciled = response.get_json()["data"]["payment"]
        assert reconciled["status"] == "Completed"

        # Wallet still debited exactly once (no double debit, no refund)
        assert wallet_balance(user["id"]) == Decimal("500.00")

        # Repeated reconcile is a no-op and does not change the balance.
        again = client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers)
        assert again.status_code == 200
        assert again.get_json()["data"]["payment"]["status"] == "Completed"
        assert wallet_balance(user["id"]) == Decimal("500.00")

    def test_reconcile_already_completed_is_noop(self, client, authenticated_user):
        _, headers = authenticated_user(email="recon2@example.com", balance="1000.00")

        # Create a completed payment
        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment_id = create_resp.get_json()["data"]["payment"]["id"]

        # Reconcile is idempotent on terminal states
        response = client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers)
        assert response.status_code == 200
        assert response.get_json()["data"]["payment"]["status"] == "Completed"

    def test_cannot_reconcile_other_users_payment(self, client, authenticated_user):
        _, headers_a = authenticated_user(email="recon_a@example.com", balance="1000.00")

        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": PENDING_ACCOUNT,
                "amount": 200,
            },
            headers=headers_a,
        )
        payment_id = create_resp.get_json()["data"]["payment"]["id"]

        _, headers_b = authenticated_user(email="recon_b@example.com", balance="1000.00")

        response = client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers_b)
        assert response.status_code == 404

    def test_reconcile_nonexistent_returns_404(self, client, authenticated_user):
        _, headers = authenticated_user(email="recon_none@example.com", balance="1000.00")
        response = client.post(f"{PAYMENTS_URL}/99999/reconcile", headers=headers)
        assert response.status_code == 404


# ===================================================================
# SECTION 3: Financial Integrity Tests
# ===================================================================


class TestFinancialIntegrity:
    """Ensure wallet, ledger, and transaction records are consistent."""

    def test_wallet_debited_on_success(self, client, authenticated_user, wallet_balance, app):
        user, headers = authenticated_user(email="fin1@example.com", balance="2000.00")

        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert wallet_balance(user["id"]) == Decimal("1500.00")

        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user["id"]).first()
            ledger_entries = WalletLedger.query.filter_by(wallet_id=wallet.id).all()
            debits = [e for e in ledger_entries if e.entry_type == LedgerEntryType.DEBIT]
            assert len(debits) == 1
            assert debits[0].amount == Decimal("500.00")

    def test_refund_on_failure_restores_balance(
        self, client, authenticated_user, wallet_balance, app
    ):
        user, headers = authenticated_user(email="fin2@example.com", balance="2000.00")

        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": FAILED_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        # Balance fully restored
        assert wallet_balance(user["id"]) == Decimal("2000.00")

        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user["id"]).first()
            ledger_entries = WalletLedger.query.filter_by(wallet_id=wallet.id).all()
            debits = [e for e in ledger_entries if e.entry_type == LedgerEntryType.DEBIT]
            credits = [e for e in ledger_entries if e.entry_type == LedgerEntryType.CREDIT]
            # One debit (attempt) + one credit (refund)
            assert len(debits) == 1
            assert len(credits) == 1
            assert debits[0].amount == Decimal("500.00")
            assert credits[0].amount == Decimal("500.00")

    def test_pending_payment_creates_single_debit(
        self, client, authenticated_user, wallet_balance, app
    ):
        user, headers = authenticated_user(email="fin3@example.com", balance="2000.00")

        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": PENDING_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        assert wallet_balance(user["id"]) == Decimal("1500.00")

        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user["id"]).first()
            ledger_entries = WalletLedger.query.filter_by(wallet_id=wallet.id).all()
            debits = [e for e in ledger_entries if e.entry_type == LedgerEntryType.DEBIT]
            assert len(debits) == 1

    def test_reconciliation_does_not_create_double_debit(
        self, client, authenticated_user, wallet_balance, app
    ):
        user, headers = authenticated_user(email="fin4@example.com", balance="2000.00")

        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": PENDING_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment_id = create_resp.get_json()["data"]["payment"]["id"]

        # Reconcile multiple times
        for _ in range(3):
            client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers)

        # Still only one debit
        assert wallet_balance(user["id"]) == Decimal("1500.00")

        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user["id"]).first()
            ledger_entries = WalletLedger.query.filter_by(wallet_id=wallet.id).all()
            debits = [e for e in ledger_entries if e.entry_type == LedgerEntryType.DEBIT]
            assert len(debits) == 1

    def test_reconciliation_idempotent_on_terminal_state(
        self, client, authenticated_user, wallet_balance
    ):
        _, headers = authenticated_user(email="fin5@example.com", balance="1000.00")

        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment_id = create_resp.get_json()["data"]["payment"]["id"]

        # Reconcile an already-completed payment
        for _ in range(3):
            resp = client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers)
            assert resp.status_code == 200
            assert resp.get_json()["data"]["payment"]["status"] == "Completed"

    def test_ledger_entries_have_correct_references(self, client, authenticated_user, app):
        _, headers = authenticated_user(email="fin6@example.com", balance="2000.00")

        create_resp = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment_ref = create_resp.get_json()["data"]["payment"]["payment_reference"]

        with app.app_context():
            entry = WalletLedger.query.filter_by(reference=payment_ref).first()
            assert entry is not None
            assert entry.entry_type == LedgerEntryType.DEBIT
            assert entry.amount == Decimal("500.00")

    def test_payment_reference_is_unique(self, client, authenticated_user):
        _, headers = authenticated_user(email="fin7@example.com", balance="5000.00")

        refs = set()
        for _ in range(5):
            resp = client.post(
                PAYMENTS_URL,
                json={
                    "service_type": "ELECTRICITY",
                    "account_number": SUCCESS_ACCOUNT,
                    "amount": 100,
                },
                headers=headers,
            )
            ref = resp.get_json()["data"]["payment"]["payment_reference"]
            assert ref not in refs
            refs.add(ref)

    def test_multiple_service_types_debit_correctly(
        self, client, authenticated_user, wallet_balance, app
    ):
        user, headers = authenticated_user(email="fin8@example.com", balance="5000.00")

        # Electricity
        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        # Water
        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "WATER",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 300,
            },
            headers=headers,
        )

        # Airtime
        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "AIRTIME",
                "account_number": "254111111111",
                "amount": 200,
            },
            headers=headers,
        )

        assert wallet_balance(user["id"]) == Decimal("4000.00")

        with app.app_context():
            service_payments = ServicePayment.query.filter_by(user_id=user["id"]).all()
            assert len(service_payments) == 3
            types = {sp.service_type for sp in service_payments}
            assert types == {"ELECTRICITY", "WATER", "AIRTIME"}


# ===================================================================
# SECTION 4: Regression Tests
# ===================================================================


class TestRegression:
    """Ensure the service payment feature doesn't break existing functionality."""

    def test_wallet_endpoint_still_works(self, client, authenticated_user):
        _, headers = authenticated_user(email="reg1@example.com", balance="500.00")
        response = client.get("/api/wallet", headers=headers)
        assert response.status_code == 200

    def test_transaction_history_still_works(self, client, authenticated_user):
        _, headers = authenticated_user(email="reg2@example.com", balance="500.00")
        response = client.get("/api/transactions", headers=headers)
        assert response.status_code == 200

    def test_beneficiary_endpoints_still_work(self, client, authenticated_user):
        _, headers = authenticated_user(email="reg3@example.com", balance="500.00")
        response = client.get("/api/beneficiaries", headers=headers)
        assert response.status_code == 200

    def test_service_payments_appear_in_transaction_history(
        self, client, authenticated_user, app
    ):
        _, headers = authenticated_user(email="reg4@example.com", balance="1000.00")

        client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": SUCCESS_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )

        response = client.get("/api/transactions", headers=headers)
        assert response.status_code == 200
        transactions = response.get_json()["data"]["transactions"]
        service_txs = [t for t in transactions if t["tx_type"] == "ServicePayment"]
        assert len(service_txs) == 1


# ===================================================================
# SECTION 5: Model Tests
# ===================================================================


class TestServicePaymentModel:
    """Test model serialization and helper methods."""

    def test_to_dict_masks_account_number(self, app):
        with app.app_context():
            sp = ServicePayment(
                user_id=1,
                wallet_id=1,
                service_type="ELECTRICITY",
                account_number="1234567890",
                amount=Decimal("500.00"),
                status=ServicePaymentStatus.COMPLETED,
                payment_reference="VYL-SVC-TEST01",
            )
            data = sp.to_dict()
            assert data["account_number"] == "****7890"

    def test_service_payment_status_terminal(self):
        assert ServicePaymentStatus.is_terminal(ServicePaymentStatus.COMPLETED)
        assert ServicePaymentStatus.is_terminal(ServicePaymentStatus.FAILED)
        assert ServicePaymentStatus.is_terminal(ServicePaymentStatus.REFUNDED)
        assert not ServicePaymentStatus.is_terminal(ServicePaymentStatus.PENDING)
        assert not ServicePaymentStatus.is_terminal(ServicePaymentStatus.PROCESSING)

    def test_service_payment_status_recoverable(self):
        assert ServicePaymentStatus.is_recoverable(ServicePaymentStatus.INITIATED)
        assert ServicePaymentStatus.is_recoverable(ServicePaymentStatus.PROCESSING)
        assert ServicePaymentStatus.is_recoverable(ServicePaymentStatus.PENDING)
        assert not ServicePaymentStatus.is_recoverable(ServicePaymentStatus.COMPLETED)
        assert not ServicePaymentStatus.is_recoverable(ServicePaymentStatus.FAILED)
        assert not ServicePaymentStatus.is_recoverable(ServicePaymentStatus.REFUNDED)

    def test_service_type_all(self):
        assert "ELECTRICITY" in ServiceType.ALL
        assert "WATER" in ServiceType.ALL
        assert "AIRTIME" in ServiceType.ALL
        assert len(ServiceType.ALL) == 3


# ===================================================================
# SECTION 6: Idempotency & Concurrency Regression Tests
# ===================================================================


class TestIdempotency:
    """Duplicate submissions must not debit the wallet twice."""

    def test_duplicate_request_same_key_debits_once(
        self, client, authenticated_user, wallet_balance, app
    ):
        user, headers = authenticated_user(email="idem1@example.com", balance="1000.00")
        payload = {
            "service_type": "ELECTRICITY",
            "account_number": SUCCESS_ACCOUNT,
            "amount": 500,
            "idempotency_key": "idem-key-001",
        }
        r1 = client.post(PAYMENTS_URL, json=payload, headers=headers)
        r2 = client.post(PAYMENTS_URL, json=payload, headers=headers)

        assert r1.status_code == 201
        assert r2.status_code == 201

        p1 = r1.get_json()["data"]["payment"]
        p2 = r2.get_json()["data"]["payment"]
        # Same payment returned both times.
        assert p1["id"] == p2["id"]
        assert p1["payment_reference"] == p2["payment_reference"]

        # Exactly one debit -> balance 500, not 0.
        assert wallet_balance(user["id"]) == Decimal("500.00")

        with app.app_context():
            from app.models import ServicePayment

            assert ServicePayment.query.filter_by(user_id=user["id"]).count() == 1

    def test_different_keys_create_separate_payments(
        self, client, authenticated_user, wallet_balance
    ):
        user, headers = authenticated_user(email="idem2@example.com", balance="2000.00")
        base = {"service_type": "ELECTRICITY", "account_number": SUCCESS_ACCOUNT, "amount": 500}
        r1 = client.post(PAYMENTS_URL, json={**base, "idempotency_key": "k-A"}, headers=headers)
        r2 = client.post(PAYMENTS_URL, json={**base, "idempotency_key": "k-B"}, headers=headers)

        assert r1.status_code == 201 and r2.status_code == 201
        assert r1.get_json()["data"]["payment"]["id"] != r2.get_json()["data"]["payment"]["id"]
        # Two distinct debits.
        assert wallet_balance(user["id"]) == Decimal("1000.00")

    def test_reconcile_refund_ledger_links_transaction(
        self, client, authenticated_user, app
    ):
        """A reconciled refund must link its ledger entry to the transaction."""
        from app.models import LedgerEntryType, ServicePayment, Transaction, Wallet

        user, headers = authenticated_user(email="idem3@example.com", balance="1000.00")

        create = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": PENDING_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment_id = create.get_json()["data"]["payment"]["id"]

        # Force the provider to FAIL on reconcile by patching its process method.
        from app.services.providers.electricity import ElectricityProvider

        original = ElectricityProvider.process

        def _fail(cls, account_number, amount, payment_reference):
            from app.services.providers.base import ProviderResult

            return ProviderResult(
                status="FAILED",
                failure_reason="Injected failure for test.",
            )

        ElectricityProvider.process = classmethod(_fail)
        try:
            resp = client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers)
            assert resp.status_code == 200
            assert resp.get_json()["data"]["payment"]["status"] == "Refunded"
        finally:
            ElectricityProvider.process = original

        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user["id"]).first()
            credits = WalletLedger.query.filter_by(
                wallet_id=wallet.id, entry_type=LedgerEntryType.CREDIT
            ).all()
            assert len(credits) == 1
            # Refund ledger entry must be tied to the transaction for audit.
            assert credits[0].transaction_id is not None
            tx = db.session.get(Transaction, credits[0].transaction_id)
            assert tx is not None
            assert tx.tx_type == TransactionType.SERVICE_PAYMENT


class TestConcurrentReconcileSafety:
    """Concurrent reconciles must not double-refund (money integrity)."""

    def test_concurrent_reconcile_single_refund(
        self, client, authenticated_user, wallet_balance, app
    ):
        from app.models import LedgerEntryType, ServicePayment, Wallet

        user, headers = authenticated_user(email="conc1@example.com", balance="1000.00")

        create = client.post(
            PAYMENTS_URL,
            json={
                "service_type": "ELECTRICITY",
                "account_number": PENDING_ACCOUNT,
                "amount": 500,
            },
            headers=headers,
        )
        payment_id = create.get_json()["data"]["payment"]["id"]

        from app.services.providers.electricity import ElectricityProvider

        original = ElectricityProvider.process

        def _fail(cls, account_number, amount, payment_reference):
            from app.services.providers.base import ProviderResult

            return ProviderResult(
                status="FAILED",
                failure_reason="Injected failure for concurrency test.",
            )

        ElectricityProvider.process = classmethod(_fail)
        try:
            # Fire several reconciles "concurrently" (sequential here, but each
            # re-locks the payment row and re-checks terminal state).
            statuses = []
            for _ in range(4):
                r = client.post(f"{PAYMENTS_URL}/{payment_id}/reconcile", headers=headers)
                assert r.status_code == 200
                statuses.append(r.get_json()["data"]["payment"]["status"])
        finally:
            ElectricityProvider.process = original

        # Final state must be Refunded (terminal) and only one refund applied.
        assert statuses[-1] == "Refunded"
        assert wallet_balance(user["id"]) == Decimal("1000.00")  # debited then refunded

        with app.app_context():
            wallet = Wallet.query.filter_by(user_id=user["id"]).first()
            credits = WalletLedger.query.filter_by(
                wallet_id=wallet.id, entry_type=LedgerEntryType.CREDIT
            ).all()
            # Exactly one refund credit regardless of how many reconciles ran.
            assert len(credits) == 1

