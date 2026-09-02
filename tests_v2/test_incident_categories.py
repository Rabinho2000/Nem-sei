"""The fault/communication/coverage split, and the guarantee it exists for:
a monitoring-coverage gap must never be presentable as a real fault.
"""
from __future__ import annotations

from nemsei.diagnostics.findings import RULES_VERSION  # noqa: F401 -- forces findings.py to have loaded, so rule_code drift below is meaningful
from nemsei.diagnostics.incident_categories import (
    CATEGORY_LABELS,
    CATEGORY_TONES,
    INCIDENT_CATEGORIES,
    incident_category,
    is_real_fault,
)


# Every real rule_code this codebase's findings.py can actually produce,
# copied by hand from a grep over `rule_code="..."` literals -- if a new rule
# is added there and not here, `test_every_known_rule_code_is_categorized`
# is the guard that catches it before it ships uncategorized.
KNOWN_RULE_CODES = {
    "daily_energy_disparity_among_peers",
    "device_no_history",
    "device_unavailable",
    "device_unknown_status",
    "partial_device_coverage",
    "plant_fault",
    "plant_offline",
    "plant_state_stale",
    "plant_warning",
    "power_disparity_among_peers",
    "stale_reading",
    "zero_power_while_peers_active",
    "zero_production_in_productive_window",
}


def test_every_known_rule_code_is_categorized_into_a_valid_category() -> None:
    for code in KNOWN_RULE_CODES:
        assert incident_category(code) in INCIDENT_CATEGORIES


def test_the_94_percent_case_is_coverage_not_fault() -> None:
    """The finding that started this module: `stale_reading` and
    `device_unknown_status` were 422 of 447 real open incidents, and neither
    is an equipment failure."""
    assert incident_category("stale_reading") == "coverage"
    assert incident_category("device_unknown_status") == "coverage"
    assert is_real_fault("stale_reading") is False
    assert is_real_fault("device_unknown_status") is False


def test_a_device_that_never_reported_is_coverage_not_fault() -> None:
    assert incident_category("device_no_history") == "coverage"
    assert is_real_fault("device_no_history") is False


def test_partial_fleet_coverage_is_coverage() -> None:
    assert incident_category("partial_device_coverage") == "coverage"


def test_a_confirmed_unavailable_device_is_a_real_fault() -> None:
    assert incident_category("device_unavailable") == "fault"
    assert is_real_fault("device_unavailable") is True


def test_a_provider_reported_fault_is_a_real_fault() -> None:
    assert incident_category("plant_fault") == "fault"


def test_zero_production_in_daylight_is_a_real_fault() -> None:
    assert incident_category("zero_production_in_productive_window") == "fault"


def test_peer_disparity_rules_are_faults() -> None:
    assert incident_category("zero_power_while_peers_active") == "fault"
    assert incident_category("power_disparity_among_peers") == "fault"
    assert incident_category("daily_energy_disparity_among_peers") == "fault"


def test_provider_connectivity_rules_are_communication_not_fault() -> None:
    """The plant behind a `plant_offline` reading may be perfectly fine --
    what failed is hearing from it."""
    assert incident_category("plant_offline") == "communication"
    assert incident_category("plant_state_stale") == "communication"
    assert is_real_fault("plant_offline") is False


def test_an_unrecognized_rule_code_resolves_to_fault_not_silently_to_coverage() -> None:
    """The safer direction to be wrong in: over-alerting on an uncategorized
    rule, not filing an unknown new fault type under "just a coverage gap"
    where it could hide."""
    assert incident_category("some_future_rule_nobody_has_seen") == "fault"


def test_every_category_has_a_portuguese_label_and_a_tone() -> None:
    for category in INCIDENT_CATEGORIES:
        assert category in CATEGORY_LABELS
        assert category in CATEGORY_TONES


def test_coverage_is_never_rendered_with_the_fault_tone() -> None:
    """The tone is what actually keeps a dashboard honest at a glance --
    volume of coverage gaps must never read visually as volume of avarias."""
    assert CATEGORY_TONES["coverage"] != CATEGORY_TONES["fault"]
    assert CATEGORY_TONES["coverage"] == "muted"
    assert CATEGORY_TONES["fault"] == "danger"
