"""What needs attention first, at one installation -- an adapter, not a rule set.

Another context is building a shared `operational_priority(...)` engine for
Telegram alerts and the morning briefing, per
`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`. It will combine incident
severity, `contracts.priority.service_priority` (whose money is at risk --
ESCO under O&M outranks EPC under O&M, which outranks anything out of
scope), incident category (`diagnostics.incident_categories` -- a real fault
outranks a communication problem, and neither is out-ranked by a pile of
coverage gaps), and eventually work-order urgency. The dashboard and the
installation list must rank by that SAME function once it exists -- not by a
second, independently-tuned algorithm that will quietly drift from whatever
Telegram actually alerts on.

It does not exist yet. `installation_priority` below is the seam: every
caller in this codebase imports it from here, never invents its own
ordering, so the day the real engine lands this function's *body* is
replaced by a call into it and nothing else has to change. Until then, the
body is deliberately minimal -- built only from rules this codebase already
has and already tests elsewhere (`contracts.priority.service_priority`,
`diagnostics.incident_categories`), never a new weighting invented here.

Coverage gaps are never part of the ranking, on purpose: GOAL.md is explicit
that a monitoring gap must never present as a real problem, and letting
volume of coverage incidents move an installation up the list would be
exactly that, just done through ranking instead of through a badge.
"""
from __future__ import annotations

from dataclasses import dataclass


PRIORITY_REASONS = {
    0: "Avaria confirmada numa instalação prioritária",
    1: "Avaria confirmada",
    2: "Falha de comunicação numa instalação prioritária",
    3: "Falha de comunicação",
    4: "Sem problema operacional confirmado",
}


@dataclass(frozen=True)
class InstallationPriority:
    rank: int  # lower sorts first
    reason: str


def installation_priority(
    *, real_fault_count: int, communication_count: int, commercial_priority: str
) -> InstallationPriority:
    """A minimal, temporary stand-in for the shared engine -- see module
    docstring. `commercial_priority` is `contracts.priority.service_priority`'s
    own `"high"`/`"normal"`/`"low"`, passed in rather than recomputed here.
    """
    prioritized = commercial_priority == "high"
    if real_fault_count > 0:
        return InstallationPriority(0 if prioritized else 1, PRIORITY_REASONS[0 if prioritized else 1])
    if communication_count > 0:
        return InstallationPriority(2 if prioritized else 3, PRIORITY_REASONS[2 if prioritized else 3])
    return InstallationPriority(4, PRIORITY_REASONS[4])
