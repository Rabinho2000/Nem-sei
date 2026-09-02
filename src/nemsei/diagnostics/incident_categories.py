"""Whether an incident is a real fault, a communication problem, or just a
monitoring-coverage gap -- never let the second or third read as the first.

This is the direct answer to the finding that started the whole redesign: of
447 open incidents at the last real count, 422 (94%) were `stale_reading` or
`device_unknown_status` -- not equipment failures, but the fact that only one
asset in the whole fleet is polled live. A dashboard, an incidents list, or a
notification that shows all 447 the same way tells an operator the fleet is
on fire when it is mostly just unwatched. This module is the one place that
distinction gets made, from `rule_code` alone, so every screen that shows
incidents agrees.

Three categories, not two, because "not a real fault" is itself two
different problems with two different fixes:

    fault          equipment or a plant is confirmed broken or producing
                   wrong -- evidence says so, not absence of evidence
    communication  the provider connection itself is the problem -- the
                   plant may be fine, but nothing is coming through
    coverage       there is not enough data to say anything at all -- an
                   onboarding or polling-density gap, not a claim about the
                   installation's real state

Deliberately does not touch `findings.py`'s severity (`critical`/`warning`/
`info`) or `diagnostics/incidents.py`'s persistence policy -- those already
answer "how urgent" and "should this exist at all". This answers a third,
independent question: "what kind of problem is this, if it exists".
"""
from __future__ import annotations

INCIDENT_CATEGORIES = ("fault", "communication", "coverage")

CATEGORY_LABELS = {
    "fault": "Avaria",
    "communication": "Comunicação",
    "coverage": "Cobertura",
}

# A tone per category -- not the same as an incident's own severity tone,
# because a `coverage` incident is never `danger` regardless of how many
# there are: the whole point is that volume of monitoring gaps must not read
# as volume of avarias.
CATEGORY_TONES = {
    "fault": "danger",
    "communication": "warning",
    "coverage": "muted",
}

# Confirmed by real evidence that the equipment or the plant itself is doing
# the wrong thing -- not merely that nothing was heard from it.
_FAULT_RULES = frozenset(
    {
        "device_unavailable",
        "plant_fault",
        "zero_power_while_peers_active",
        "zero_production_in_productive_window",
        "power_disparity_among_peers",
        "daily_energy_disparity_among_peers",
    }
)

# The provider connection or the plant's own communication is the subject of
# the finding -- the equipment behind it may be perfectly fine.
_COMMUNICATION_RULES = frozenset(
    {
        "plant_offline",
        "plant_state_stale",
    }
)

# Not enough evidence to claim anything about the installation's real state:
# a device that has never reported, a reading too old to trust, a status code
# nobody has classified yet, or a fleet where some devices report and others
# do not. Every one of these is a monitoring gap, not a verdict on the plant.
_COVERAGE_RULES = frozenset(
    {
        "device_no_history",
        "stale_reading",
        "device_unknown_status",
        "partial_device_coverage",
    }
)

# `plant_warning` is deliberately not filed anywhere above: the provider
# reports a warning condition on the plant without saying what kind, so this
# module cannot honestly claim it is equipment, connectivity, or a gap. It
# resolves to `fault` below only because "the provider says something is
# wrong" is closer to a confirmed problem than to silence -- revisit if
# FusionSolar/Sigenergy ever break this code down further.
_UNCATEGORIZED_DEFAULT = "fault"


def incident_category(rule_code: str) -> str:
    """One of `INCIDENT_CATEGORIES` for a `DiagnosticIncident.rule_code`.

    An unrecognised code (a rule added to `findings.py` without being added
    here) resolves to `fault` rather than `coverage` -- the safer direction
    to be wrong in is over-alerting on an unclassified rule, not silently
    filing a possible new fault type under "just a coverage gap".
    """
    if rule_code in _FAULT_RULES:
        return "fault"
    if rule_code in _COMMUNICATION_RULES:
        return "communication"
    if rule_code in _COVERAGE_RULES:
        return "coverage"
    return _UNCATEGORIZED_DEFAULT


def is_real_fault(rule_code: str) -> bool:
    """Whether this incident describes a confirmed problem, not a monitoring
    gap. Convenience for the common "how many real faults" question --
    `incident_category(code) == "fault"` inline everywhere would work just as
    well, but this names the specific thing GOAL.md asks never to conflate."""
    return incident_category(rule_code) == "fault"
