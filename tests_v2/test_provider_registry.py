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


def test_huawei_scada_claims_only_the_capabilities_it_can_answer() -> None:
    """Each absence here is an evidenced decision, not an omission.

    Discovery would be one step from binding a dongle automatically; device
    monitoring has nothing to read while `unit=1` answers 0x83/0x04; production
    history does not exist on the wire at all -- the daily energy V2 holds is
    integrated locally from samples it already stored. See
    `docs/v2/HUAWEI_SCADA.md`.
    """
    descriptor = descriptor_for(ProviderCode.HUAWEI_SCADA)
    assert descriptor.implemented_capabilities == frozenset(
        {ProviderCapability.CURRENT_MONITORING, ProviderCapability.REALTIME_PRODUCTION}
    )


def test_no_provider_anywhere_claims_provider_mutations() -> None:
    """Read-only is a property of the whole registry, not of one adapter."""
    for provider in ProviderCode:
        assert ProviderCapability.PROVIDER_MUTATIONS not in descriptor_for(provider).implemented_capabilities


def test_a_dongle_serial_normalizes_case_insensitively() -> None:
    # Serials arrive off the wire, and a mapping approved as uppercase must
    # still match a banner that announces it in any case.
    assert normalize_external_id(ProviderCode.HUAWEI_SCADA, " hv2340123456 ") == "hv2340123456"
    assert normalize_external_id(ProviderCode.HUAWEI_SCADA, "HV2340123456") == "hv2340123456"
