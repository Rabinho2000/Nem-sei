"""Which kind of problem a `rule_code` is, for notification purposes.

Reuses `diagnostics.incident_categories` (`fault`/`communication`/`coverage`)
as the single source of truth for classifying a `rule_code` -- this module
does not maintain a second, competing table. That module was built
concurrently in this same shared working tree specifically so "every screen
that shows incidents agrees" (its own docstring); notifications is one more
such screen, not an exception.

What this module adds, specific to notifications:

- **Episode identity** (`notifications/episodes.py`): an episode is
  `(asset_id, problem_family, device_id)`. `problem_family` here *is*
  `incident_categories.incident_category(rule_code)` -- nothing renamed,
  nothing reinterpreted.
- **Briefing category** (req 12): `category_for` maps the shared
  fault/communication/coverage vocabulary onto the three-way English label
  the morning briefing needs ("Operational fault" / "Communication issue" /
  "Monitoring coverage / insufficient data"), so "1 instalação sem dados
  suficientes" never gets presented as "1 avaria".
"""
from __future__ import annotations

from nemsei.diagnostics.incident_categories import INCIDENT_CATEGORIES, incident_category

# Same vocabulary as diagnostics.incident_categories.INCIDENT_CATEGORIES --
# an episode's `problem_family` is that module's `category`, not a fourth
# value invented here. (An earlier draft of this module used a separate
# `production` family; folded away once `diagnostics/incident_categories.py`
# landed with `power_disparity_among_peers`/`daily_energy_disparity_among_
# peers`/`zero_power_while_peers_active` already correctly filed under
# `fault` -- a confirmed production loss *is* a fault, and inventing a
# second bucket for it would have been exactly the duplicated classification
# this module exists to avoid.)
PROBLEM_FAMILIES = INCIDENT_CATEGORIES

CATEGORIES = ("operational_fault", "communication_issue", "monitoring_coverage")

_CATEGORY_BY_FAMILY = {
    "fault": "operational_fault",
    "communication": "communication_issue",
    "coverage": "monitoring_coverage",
}

CATEGORY_LABELS = {
    "operational_fault": "Operational fault",
    "communication_issue": "Communication issue",
    "monitoring_coverage": "Monitoring coverage / insufficient data",
}


def problem_family_for(rule_code: str) -> str:
    return incident_category(rule_code)


def category_for(problem_family: str) -> str:
    return _CATEGORY_BY_FAMILY.get(problem_family, "operational_fault")


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)
