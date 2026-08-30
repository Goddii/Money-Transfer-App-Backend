"""Base class for simulated service providers.

Every provider must implement ``validate`` and ``process``. The base class
defines the deterministic scenario logic that all providers share.
"""

import secrets
import string
from decimal import Decimal

from app.utils.errors import ApiError, ErrorCode


class ProviderResult:
    """Outcome returned by a simulated provider."""

    def __init__(self, status, metadata=None, failure_reason=None):
        self.status = status
        self.metadata = metadata or {}
        self.failure_reason = failure_reason


# Deterministic scenario account numbers. Documented in docs/SERVICE_PAYMENT_SCENARIOS.md.
# These apply across all service types. The account number's last 10 digits
# (or the full number if shorter) are checked against these prefixes.
SCENARIO_SUCCESS_PREFIXES = ("1111111111", "1111")
SCENARIO_PENDING_PREFIXES = ("2222222222", "2222")
SCENARIO_FAILED_PREFIXES = ("3333333333", "3333")


def _determine_scenario(account_number):
    """Determine the payment scenario from the account number.

    Returns "SUCCESS", "PENDING", or "FAILED".
    """
    normalized = account_number.strip()

    for prefix in SCENARIO_FAILED_PREFIXES:
        if normalized.startswith(prefix):
            return "FAILED"

    for prefix in SCENARIO_PENDING_PREFIXES:
        if normalized.startswith(prefix):
            return "PENDING"

    for prefix in SCENARIO_SUCCESS_PREFIXES:
        if normalized.startswith(prefix):
            return "SUCCESS"

    # Default: success (makes normal demo flow smooth).
    return "SUCCESS"


def _generate_payment_reference(prefix="VYL-SVC"):
    """Generate a unique payment reference like VYL-SVC-A1B2C3."""
    random_part = secrets.token_hex(3).upper()
    return f"{prefix}-{random_part}"


class BaseProvider:
    """Abstract base for simulated service providers."""

    SERVICE_TYPE = None  # Override in subclass.
    ACCOUNT_LABEL = "Account number"  # Override for display.
    ACCOUNT_MIN_LENGTH = 10
    ACCOUNT_MAX_LENGTH = 15

    @classmethod
    def validate(cls, account_number, amount):
        """Validate the account number and amount.

        Returns the cleaned/normalized account number.
        Raises ApiError on validation failure.
        """
        if not account_number or not isinstance(account_number, str):
            raise ApiError(
                f"{cls.ACCOUNT_LABEL} is required.",
                400,
                ErrorCode.INVALID_ACCOUNT_NUMBER,
            )

        cleaned = account_number.strip()

        if not cleaned.isdigit():
            raise ApiError(
                f"{cls.ACCOUNT_LABEL} must contain digits only.",
                400,
                ErrorCode.INVALID_ACCOUNT_NUMBER,
            )

        if len(cleaned) < cls.ACCOUNT_MIN_LENGTH or len(cleaned) > cls.ACCOUNT_MAX_LENGTH:
            raise ApiError(
                f"{cls.ACCOUNT_LABEL} must be between {cls.ACCOUNT_MIN_LENGTH} and {cls.ACCOUNT_MAX_LENGTH} digits.",
                400,
                ErrorCode.INVALID_ACCOUNT_NUMBER,
            )

        if amount is None:
            raise ApiError("Amount is required.", 400, ErrorCode.INVALID_AMOUNT)

        try:
            amount = Decimal(str(amount))
        except (ArithmeticError, ValueError):
            raise ApiError(
                "Amount must be a valid number.",
                400,
                ErrorCode.INVALID_AMOUNT,
            )

        if amount <= 0:
            raise ApiError(
                "Amount must be greater than zero.",
                400,
                ErrorCode.INVALID_AMOUNT,
            )

        return cleaned

    @classmethod
    def process(cls, account_number, amount, payment_reference):
        """Process the simulated payment.

        Returns a ProviderResult with status, metadata, and optional failure_reason.
        """
        scenario = _determine_scenario(account_number)

        if scenario == "FAILED":
            return ProviderResult(
                status="FAILED",
                failure_reason="Simulated provider failure: payment could not be processed.",
            )

        if scenario == "PENDING":
            return ProviderResult(
                status="PENDING",
                metadata={"pending_reason": "Payment is being verified by the provider."},
            )

        # SUCCESS: subclass provides specific metadata.
        return cls._build_success_result(account_number, amount, payment_reference)

    @classmethod
    def _build_success_result(cls, account_number, amount, payment_reference):
        """Build a successful result. Subclasses override for service-specific fields."""
        return ProviderResult(
            status="COMPLETED",
            metadata={"payment_reference": payment_reference},
        )
