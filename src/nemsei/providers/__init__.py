"""Provider-neutral contracts and registry; no HTTP clients live here."""

from nemsei.providers.registry import (
    CapabilityAvailability,
    CapabilityStatus,
    ImplementationSupport,
    ProviderCapability,
    ProviderCode,
    RuntimeAvailability,
    descriptor_for,
    evaluate_capability,
    normalize_external_id,
)

__all__ = [
    "CapabilityAvailability",
    "CapabilityStatus",
    "ImplementationSupport",
    "ProviderCapability",
    "ProviderCode",
    "RuntimeAvailability",
    "descriptor_for",
    "evaluate_capability",
    "normalize_external_id",
]
