"""Simulated Electricity provider.

Mimics a prepaid electricity token purchase. On success, generates a
random token string and simulated units purchased.

Demo scenarios (deterministic by account number):
    1111111111 -> SUCCESS
    2222222222 -> PENDING
    3333333333 -> FAILED
"""

import secrets
import string
from decimal import Decimal

from app.services.providers.base import BaseProvider, ProviderResult


class ElectricityProvider(BaseProvider):
    SERVICE_TYPE = "ELECTRICITY"
    ACCOUNT_LABEL = "Meter number"
    ACCOUNT_MIN_LENGTH = 10
    ACCOUNT_MAX_LENGTH = 15

    # Simulated pricing: 1 KES = 0.0136 kWh (not real KPLC pricing).
    SIMULATED_RATE = Decimal("0.0136")

    @classmethod
    def _build_success_result(cls, account_number, amount, payment_reference):
        """Generate a simulated electricity token and units."""
        # Deterministic-ish token: random digits formatted as groups of 4.
        token = _generate_token()
        units = (Decimal(str(amount)) * cls.SIMULATED_RATE).quantize(Decimal("0.01"))

        return ProviderResult(
            status="COMPLETED",
            metadata={
                "payment_reference": payment_reference,
                "token": token,
                "units": float(units),
                "meter_number_masked": _mask_meter(account_number),
            },
        )


def _generate_token():
    """Generate a 20-digit electricity token formatted as XXXX-XXXX-XXXX-XXXX-XXXX."""
    digits = "".join(secrets.choice(string.digits) for _ in range(20))
    return "-".join(digits[i : i + 4] for i in range(0, 20, 4))


def _mask_meter(meter_number):
    """Mask meter number keeping last 4 digits."""
    if len(meter_number) <= 4:
        return "****"
    return f"{'*' * (len(meter_number) - 4)}{meter_number[-4:]}"


