"""Provider-neutral contracts and registry; no HTTP clients live here."""

from nemsei.providers.registry import (
    CapabilityAvailability,
    ProviderCapability,
    ProviderCode,
    descriptor_for,
    evaluate_capability,
    normalize_external_id,
)

__all__ = [
    "CapabilityAvailability",
    "ProviderCapability",
    "ProviderCode",
    "descriptor_for",
    "evaluate_capability",
    "normalize_external_id",
]
