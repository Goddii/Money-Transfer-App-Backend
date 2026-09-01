"""Simulated Water provider.

Mimics a water bill payment. On success, generates a receipt number.

Demo scenarios (deterministic by account number):
    1111111111 -> SUCCESS
    2222222222 -> PENDING
    3333333333 -> FAILED
"""

import secrets
import string

from app.services.providers.base import BaseProvider, ProviderResult


class WaterProvider(BaseProvider):
    SERVICE_TYPE = "WATER"
    ACCOUNT_LABEL = "Water account number"
    ACCOUNT_MIN_LENGTH = 10
    ACCOUNT_MAX_LENGTH = 15

    @classmethod
    def _build_success_result(cls, account_number, amount, payment_reference):
        """Generate a simulated water payment receipt."""
        receipt = _generate_receipt()
        return ProviderResult(
            status="COMPLETED",
            metadata={
                "payment_reference": payment_reference,
                "receipt_number": receipt,
                "account_number_masked": _mask_account(account_number),
            },
        )


def _generate_receipt():
    """Generate a receipt number like VYL-WTR-A1B2C3."""
    random_part = secrets.token_hex(3).upper()
    return f"VYL-WTR-{random_part}"


def _mask_account(account_number):
    """Mask account number keeping last 4 digits."""
    if len(account_number) <= 4:
        return "****"
    return f"{'*' * (len(account_number) - 4)}{account_number[-4:]}"
