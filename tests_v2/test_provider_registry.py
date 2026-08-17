from __future__ import annotations

import pytest

from nemsei.providers import (
    CapabilityAvailability,
    ProviderCapability,
    ProviderCode,
    descriptor_for,
    evaluate_capability,
    normalize_external_id,
)


@pytest.mark.parametrize("provider", list(ProviderCode))
def test_registry_has_all_first_class_provider_descriptors(provider: ProviderCode) -> None:
    assert descriptor_for(provider).code is provider


def test_sigenergy_identifier_normalization_is_case_insensitive() -> None:
    assert normalize_external_id(ProviderCode.SIGENERGY, " Sig-1 ") == "sig-1"


def test_unconfigured_connections_never_claim_a_live_capability() -> None:
    assert evaluate_capability(
        ProviderCode.SMA,
        ProviderCapability.PRODUCTION_HISTORY,
        connection_configured=False,
    ).runtime_availability is CapabilityAvailability.NOT_CONFIGURED


def test_fusionsolar_and_sigenergy_discovery_are_runtime_available_when_configured() -> None:
    assert evaluate_capability(
        ProviderCode.FUSIONSOLAR,
        ProviderCapability.DISCOVERY,
        connection_configured=True,
    ).runtime_availability is CapabilityAvailability.AVAILABLE
    assert evaluate_capability(
        ProviderCode.SIGENERGY,
        ProviderCapability.DISCOVERY,
        connection_configured=True,
    ).runtime_availability is CapabilityAvailability.AVAILABLE


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        descriptor_for("unknown")
