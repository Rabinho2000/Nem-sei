from __future__ import annotations

import pytest

from nemsei.safety.external_actions import CapabilityPolicy, ExternalActionDenied


def test_capability_policy_is_default_deny(settings) -> None:
    policy = CapabilityPolicy(settings.capabilities)
    for capability in settings.capabilities:
        with pytest.raises(ExternalActionDenied):
            policy.require(capability)


def test_shadow_environment_can_allow_provider_reads_only(settings) -> None:
    values = dict(settings.capabilities)
    values["provider_reads"] = True
    policy = CapabilityPolicy(values)
    policy.require("provider_reads")
    with pytest.raises(ExternalActionDenied):
        policy.require("provider_mutations")
