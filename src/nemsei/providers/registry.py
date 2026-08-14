"""Static provider vocabulary for V2; adapter behavior is deliberately absent."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable


class ProviderCode(StrEnum):
    FUSIONSOLAR = "fusionsolar"
    SIGENERGY = "sigenergy"
    SMA = "sma"


class ProviderCapability(StrEnum):
    DISCOVERY = "discovery"
    PLANT_METADATA = "plant_metadata"
    CURRENT_MONITORING = "current_monitoring"
    DEVICE_MONITORING = "device_monitoring"
    ALARMS = "alarms"
    PRODUCTION_HISTORY = "production_history"
    REALTIME_PRODUCTION = "realtime_production"
    DEVICE_DISCOVERY = "device_discovery"
    REPORT_DATA = "report_data"
    PROVIDER_MUTATIONS = "provider_mutations"


class CapabilityAvailability(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class ProviderDescriptor:
    code: ProviderCode
    display_name: str
    identifier_normalizer: Callable[[str], str]
    documented_capabilities: frozenset[ProviderCapability]


def _trim(value: str) -> str:
    return value.strip()


def _casefold(value: str) -> str:
    return value.strip().casefold()


_DESCRIPTORS = {
    ProviderCode.FUSIONSOLAR: ProviderDescriptor(
        ProviderCode.FUSIONSOLAR,
        "FusionSolar",
        _trim,
        frozenset(),
    ),
    ProviderCode.SIGENERGY: ProviderDescriptor(
        ProviderCode.SIGENERGY,
        "Sigenergy",
        _casefold,
        frozenset(),
    ),
    ProviderCode.SMA: ProviderDescriptor(
        ProviderCode.SMA,
        "SMA",
        _trim,
        frozenset(),
    ),
}


def descriptor_for(provider: ProviderCode | str) -> ProviderDescriptor:
    try:
        return _DESCRIPTORS[ProviderCode(str(provider).lower())]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Unknown provider: {provider!r}") from exc


def normalize_external_id(provider: ProviderCode | str, value: str) -> str:
    normalized = descriptor_for(provider).identifier_normalizer(str(value))
    if not normalized:
        raise ValueError("Provider external identifier is required.")
    return normalized


def evaluate_capability(
    provider: ProviderCode | str,
    capability: ProviderCapability | str,
    *,
    connection_configured: bool,
    integration_temporarily_unavailable: bool = False,
) -> CapabilityAvailability:
    descriptor = descriptor_for(provider)
    requested = ProviderCapability(capability)
    if not connection_configured:
        return CapabilityAvailability.NOT_CONFIGURED
    if requested not in descriptor.documented_capabilities:
        return CapabilityAvailability.UNSUPPORTED
    if integration_temporarily_unavailable:
        return CapabilityAvailability.TEMPORARILY_UNAVAILABLE
    return CapabilityAvailability.SUPPORTED
