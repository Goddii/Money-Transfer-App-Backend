"""Simulated service provider abstraction.

Each provider implements the ``BaseProvider`` interface. The provider
resolver maps a ``ServiceType`` string to the correct provider class.

All providers are deterministic: the outcome is determined by the
account number pattern, not by randomness. This makes the feature
reliable for demos and tests.
"""

from app.services.providers.base import BaseProvider
from app.services.providers.electricity import ElectricityProvider
from app.services.providers.water import WaterProvider
from app.services.providers.airtime import AirtimeProvider

# Registry: service_type -> provider class.
_REGISTRY = {
    "ELECTRICITY": ElectricityProvider,
    "WATER": WaterProvider,
    "AIRTIME": AirtimeProvider,
}


def resolve_provider(service_type):
    """Return the provider class for the given service type.

    Raises ``ApiError`` if the service type is unknown or inactive.
    """
    from app.utils.errors import ApiError, ErrorCode

    provider_cls = _REGISTRY.get(service_type)

    if not provider_cls:
        raise ApiError(
            f"Unknown service type: {service_type}",
            400,
            ErrorCode.INVALID_SERVICE_TYPE,
        )

    return provider_cls


__all__ = [
    "BaseProvider",
    "ElectricityProvider",
    "WaterProvider",
    "AirtimeProvider",
    "resolve_provider",
]
