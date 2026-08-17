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
    CONNECTION_VALIDATION = "connection_validation"
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


class ImplementationSupport(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class RuntimeAvailability(StrEnum):
    AVAILABLE = "available"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"


# Backward-compatible import name for the old foundation vocabulary. New code
# uses the two separate enums above.
CapabilityAvailability = RuntimeAvailability


@dataclass(frozen=True)
class CapabilityStatus:
    implementation_support: ImplementationSupport
    runtime_availability: RuntimeAvailability


@dataclass(frozen=True)
class ProviderDescriptor:
    code: ProviderCode
    display_name: str
    identifier_normalizer: Callable[[str], str]
    implemented_capabilities: frozenset[ProviderCapability]


def _trim(value: str) -> str:
    return value.strip()


def _casefold(value: str) -> str:
    return value.strip().casefold()


_DESCRIPTORS = {
    ProviderCode.FUSIONSOLAR: ProviderDescriptor(
        ProviderCode.FUSIONSOLAR,
        "FusionSolar",
        _trim,
        frozenset({
            ProviderCapability.CONNECTION_VALIDATION,
            ProviderCapability.DISCOVERY,
            ProviderCapability.CURRENT_MONITORING,
        }),
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
    runtime_known: bool = True,
) -> CapabilityStatus:
    descriptor = descriptor_for(provider)
    requested = ProviderCapability(capability)
    support = (
        ImplementationSupport.SUPPORTED
        if requested in descriptor.implemented_capabilities
        else ImplementationSupport.UNSUPPORTED
    )
    if not connection_configured:
        availability = RuntimeAvailability.NOT_CONFIGURED
    elif integration_temporarily_unavailable:
        availability = RuntimeAvailability.TEMPORARILY_UNAVAILABLE
    elif not runtime_known:
        availability = RuntimeAvailability.UNKNOWN
    elif support is ImplementationSupport.SUPPORTED:
        availability = RuntimeAvailability.AVAILABLE
    else:
        availability = RuntimeAvailability.UNKNOWN
    return CapabilityStatus(support, availability)
