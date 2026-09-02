"""Operational priority for a notification episode -- explainable, not a
magic number.

Req 5: "Não guardar um número mágico se uma função calculada for
suficiente. [...] Nunca esconder a causa do score." Every component below is
a small, named, pure function returning `(points, reason | None)`; the total
score and the list of surfaced reasons are the *same* computation walking the
same list, so "why is this prioritised" can never drift out of sync with
"what actually happened" the way a single opaque formula could.

Nothing here touches a session. `PriorityInputs` is what
`notifications/enrichment.py` (the one module in this pipeline allowed to
query the database) builds; everything downstream of it -- this module, the
morning briefing's sort order, the Telegram renderer -- is a plain function
of plain values, testable without Postgres.

Reuses rather than reinvents wherever this codebase already answers a
component's question: `contracts.priority.describe` for the ESCO/EPC
component (whose money is at risk), `diagnostics.incidents.recurrence_count`
(passed in as a plain int) for "recorrente >=3 episódios/24h" -- counted by
distinct episodes, not raw fact rows, the same discipline
`DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` already established.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

PRIORITY_BUCKETS = ("HIGH", "MEDIUM", "LOW")
# Chosen against the component weights below, not tuned to a particular
# installation: a bare communication or fault signal alone (40 or 50 points)
# lands at MEDIUM; it takes a second real factor (ESCO, no WorkOrder,
# recurrence, a calculable and material impact) to cross into HIGH.
HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 30

# "+4h aberto" (req 5's own suggested number).
DURATION_THRESHOLD = timedelta(hours=4)
# "+impacto financeiro estimado >100€".
IMPACT_EUR_THRESHOLD = Decimal("100")
# "+potência instalada >500 kWp".
INSTALLED_POWER_THRESHOLD_KW = Decimal("500")
# "+recorrente >=3 episódios/24h".
RECURRENCE_THRESHOLD = 3


@dataclass(frozen=True)
class PriorityInputs:
    """Everything `score_episode` needs, already resolved to plain values by
    `notifications/enrichment.py`. Optional fields are optional because the
    honest answer is sometimes "unknown", not a guessed default -- see
    `notifications/impact.py` for why `impact_eur` in particular is `None`
    rather than `0` when it cannot be calculated.
    """

    problem_family: str
    severity_peak: str
    opened_at: datetime
    now: datetime
    commercial_family: str  # contracts.priority.commercial_family: esco|esco_buyout|epc|unknown
    om_status: str  # contracts.service.om_status: active|expired|undated|none
    installed_dc_power_kw: Decimal | None
    impact_eur: Decimal | None
    recurrence_count_24h: int
    has_work_order: bool
    work_planned_or_in_progress_today: bool
    recovered: bool = False


@dataclass(frozen=True)
class PriorityScore:
    score: int
    bucket: str
    reasons: list[str]


# Each component: (label, function). `label` exists only for tests that want
# to check one component in isolation without re-deriving the whole list.
_ComponentFn = Callable[[PriorityInputs], tuple[int, str | None]]


def _fault_or_offline(inputs: PriorityInputs) -> tuple[int, str | None]:
    """The two most severe signals this system can raise, both at
    problem-family granularity (there is no per-device "how much of the
    plant is affected" count to weight them apart from each other honestly,
    so this is deliberately one component, not req 5's two separate `+50`/
    `+40` line items double-counting the same underlying signal)."""
    if inputs.problem_family == "fault" and inputs.severity_peak == "critical":
        return 50, "Avaria crítica"
    if inputs.problem_family == "communication":
        return 40, "Instalação sem comunicação"
    return 0, None


def _duration(inputs: PriorityInputs) -> tuple[int, str | None]:
    age = inputs.now - inputs.opened_at
    if age >= DURATION_THRESHOLD:
        hours = int(age.total_seconds() // 3600)
        return 25, f"Aberto há mais de 4h (há {hours}h)"
    return 0, None


def _esco(inputs: PriorityInputs) -> tuple[int, str | None]:
    """Whose money is at risk, from `contracts.priority.service_priority` --
    not recomputed here. `service_priority` already returns "high" only for
    an ESCO installation under an O&M engagement in force (`active` or
    `undated`); this component just turns that into points."""
    from nemsei.contracts.priority import service_priority

    if service_priority(family=inputs.commercial_family, om_status=inputs.om_status) == "high":
        return 20, "Contrato ESCO activo — a receita perdida é da Solcor"
    return 0, None


def _financial_impact(inputs: PriorityInputs) -> tuple[int, str | None]:
    """Only ever contributes points when an impact was actually calculable
    -- `impact_eur is None` ("não calculável") is not the same as `0`, and
    must never silently read as "no impact"."""
    if inputs.impact_eur is not None and inputs.impact_eur > IMPACT_EUR_THRESHOLD:
        return 20, f"Impacto financeiro estimado > 100€ (~{inputs.impact_eur:.0f}€)"
    return 0, None


def _installed_power(inputs: PriorityInputs) -> tuple[int, str | None]:
    if inputs.installed_dc_power_kw is not None and inputs.installed_dc_power_kw > INSTALLED_POWER_THRESHOLD_KW:
        return 15, f"Potência instalada > 500 kWp ({inputs.installed_dc_power_kw:.0f} kWp)"
    return 0, None


def _no_work_order(inputs: PriorityInputs) -> tuple[int, str | None]:
    if not inputs.has_work_order:
        return 15, "Sem WorkOrder associado"
    return 0, None


def _recurrent(inputs: PriorityInputs) -> tuple[int, str | None]:
    if inputs.recurrence_count_24h >= RECURRENCE_THRESHOLD:
        return 10, f"Recorrente — {inputs.recurrence_count_24h} episódios em 24h"
    return 0, None


def _work_already_planned(inputs: PriorityInputs) -> tuple[int, str | None]:
    if inputs.work_planned_or_in_progress_today:
        return -30, "Visita/trabalho já planeado para hoje"
    return 0, None


def _already_recovered(inputs: PriorityInputs) -> tuple[int, str | None]:
    if inputs.recovered:
        return -20, "Problema já recuperado"
    return 0, None


# Ordered so `reasons` reads worst-cause-first when rendered directly --
# purely cosmetic, the sum does not depend on order.
_COMPONENTS: tuple[_ComponentFn, ...] = (
    _fault_or_offline,
    _duration,
    _esco,
    _financial_impact,
    _installed_power,
    _no_work_order,
    _recurrent,
    _work_already_planned,
    _already_recovered,
)


def score_episode(inputs: PriorityInputs) -> PriorityScore:
    total = 0
    reasons: list[str] = []
    for component in _COMPONENTS:
        points, reason = component(inputs)
        total += points
        if reason is not None:
            reasons.append(reason)
    if total >= HIGH_THRESHOLD:
        bucket = "HIGH"
    elif total >= MEDIUM_THRESHOLD:
        bucket = "MEDIUM"
    else:
        bucket = "LOW"
    return PriorityScore(score=total, bucket=bucket, reasons=reasons)
