"""Simulated Airtime provider.

Mimics an airtime purchase. On success, generates a confirmation reference.

Demo scenarios (deterministic by phone number):
    254111111111 -> SUCCESS
    254222222222 -> PENDING
    254333333333 -> FAILED
"""

import secrets

from app.services.providers.base import BaseProvider, ProviderResult
from app.utils.validators import normalize_kenyan_phone


class AirtimeProvider(BaseProvider):
    SERVICE_TYPE = "AIRTIME"
    ACCOUNT_LABEL = "Phone number"
    ACCOUNT_MIN_LENGTH = 10
    ACCOUNT_MAX_LENGTH = 15

    @classmethod
    def validate(cls, account_number, amount):
        """Validate phone number and amount.

        Phone numbers are normalized to the 2547XXXXXXXX format.
        """
        if not account_number or not isinstance(account_number, str):
            from app.utils.errors import ApiError, ErrorCode

            raise ApiError(
                "Phone number is required.",
                400,
                ErrorCode.INVALID_PHONE_NUMBER,
            )

        # Normalize the phone number. This also validates it's a Kenyan number.
        cleaned = normalize_kenyan_phone(account_number, "Phone number")

        if amount is None:
            from app.utils.errors import ApiError, ErrorCode

            raise ApiError("Amount is required.", 400, ErrorCode.INVALID_AMOUNT)

        from decimal import Decimal

        try:
            amount = Decimal(str(amount))
        except (ArithmeticError, ValueError):
            from app.utils.errors import ApiError, ErrorCode

            raise ApiError(
                "Amount must be a valid number.",
                400,
                ErrorCode.INVALID_AMOUNT,
            )

        if amount <= 0:
            from app.utils.errors import ApiError, ErrorCode

            raise ApiError(
                "Amount must be greater than zero.",
                400,
                ErrorCode.INVALID_AMOUNT,
            )

        return cleaned

    @classmethod
    def _build_success_result(cls, account_number, amount, payment_reference):
        """Generate a simulated airtime confirmation."""
        confirmation = _generate_confirmation()
        return ProviderResult(
            status="COMPLETED",
            metadata={
                "payment_reference": payment_reference,
                "confirmation_reference": confirmation,
                "phone_number_masked": _mask_phone(account_number),
            },
        )


def _generate_confirmation():
    """Generate a confirmation reference like VYL-ATM-A1B2C3."""
    random_part = secrets.token_hex(3).upper()
    return f"VYL-ATM-{random_part}"


def _mask_phone(phone_number):
    """Mask phone number keeping last 4 digits."""
    if len(phone_number) <= 4:
        return "****"
    return f"{'*' * (len(phone_number) - 4)}{phone_number[-4:]}"
