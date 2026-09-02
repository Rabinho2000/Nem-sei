"""Deterministic, explainable operational findings over device status facts.

M7 Fatia 4 (`docs/v2/DIAGNOSTICS.md`): "o que está mal nesta instalação
agora?" -- built as rules over data V2 already has (`current_device_status`,
`DeviceStatusRepository.history_for_device`), not a persisted table with an
open/acknowledged/resolved lifecycle. Deliberately: every finding here is
**recomputed fresh from current state on every call**, so the same ongoing
problem never accumulates into hundreds of independent rows -- there is
nothing to accumulate, because nothing is stored. This is the honest, simple
answer the milestone asked for ("mantém esta parte simples se ainda não
houver necessidade operacional real"); a persisted lifecycle is a real
future step, not implied by this module existing.

Every `DiagnosticFinding` answers the questions this milestone requires:
what happened (`summary`/`evidence`), where (`asset_id`/`device_id`), how
severe (`severity`), what backs it (`evidence`, with fact ids in
`fact_ids`), when it started/was last observed (`active_since`/
`observed_at`), whether it is still true right now (always `True` here --
see the module docstring), which rule produced it (`rule_code`), and what is
missing to confirm it further (`missing_data`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from nemsei.diagnostics.models import DeviceStatusFact
from nemsei.monitoring.production_window import ProductionWindow


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

# Bumped whenever a rule's *logic* changes meaningfully (a threshold, a new
# condition, a removed rule) -- persisted verbatim as `DiagnosticIncident
# .detector_version` (D1, docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md) so
# an old incident's provenance always says which rule set actually detected
# it, even after this module evolves. Not bumped for comments/refactors.
# Bumped to "2" when the plant-level rules were added: an incident opened
# before that has provenance from a rule set that could not see plant state
# at all, and saying so is the whole point of persisting the version.
RULES_VERSION = "2"

# Below this power, two readings are both "essentially off" and comparing
# their ratio would be noise (e.g. 0.02kW vs 0.05kW is a 150% "disparity"
# that means nothing). Chosen as a conservative floor, not tuned to any one
# installation's rated power.
_DISPARITY_MIN_PEER_POWER_KW = Decimal("0.5")
# A device producing less than this fraction of its most productive peer's
# power, at the same moment, is flagged -- comparable inverters at the same
# site under the same sun should track each other loosely, not exactly.
_DISPARITY_RATIO_THRESHOLD = Decimal("0.5")
_DEFAULT_STALE_AFTER = timedelta(hours=24)
# Two devices' readings are only compared to each other if they were
# observed within this much of each other -- otherwise a genuinely stale
# device would look like a "disparity" against a fresh one, which is a
# different, already-separately-flagged problem (`stale_reading`).
_COMPARISON_TOLERANCE = timedelta(minutes=45)


@dataclass(frozen=True)
class DiagnosticFinding:
    rule_code: str
    severity: str  # "critical" | "warning" | "info"
    asset_id: int
    summary: str
    evidence: dict[str, Any]
    device_id: int | None = None
    device_label: str | None = None
    observed_at: datetime | None = None
    active_since: datetime | None = None
    fact_ids: tuple[int, ...] = field(default_factory=tuple)
    missing_data: str | None = None

    @property
    def still_active(self) -> bool:
        # Always true: this module only ever returns findings computed from
        # the current snapshot. A condition that stopped being true simply
        # does not appear in the next call's list -- there is no "resolved"
        # state to track because there is no persisted row to resolve.
        return True


def evaluate_asset_findings(
    rows: list[dict[str, Any]],
    *,
    asset_id: int,
    now: datetime,
    history_for_device: Callable[[int], list[DeviceStatusFact]] | None = None,
    stale_after: timedelta = _DEFAULT_STALE_AFTER,
    production_window: ProductionWindow | None = None,
) -> list[DiagnosticFinding]:
    """Evaluate every rule for one asset's current device rows.

    `rows` is exactly the shape `diagnostics.service.current_device_status`
    already produces -- this module derives nothing from the database
    itself, it only judges values already computed elsewhere, which is what
    keeps every rule here auditable from the same evidence a human could
    read directly off the diagnostics table.

    `history_for_device`, when given, backs `active_since` for the
    single-device state rules (unavailable/unknown/no history/stale) by
    walking that device's real history rather than only looking at the
    latest reading. Without it, `active_since` falls back to `observed_at`
    (or `now`, for `device_no_history`) -- still honest, just less precise,
    and each such finding's `missing_data` says so.

    `production_window`, when given, is the one rule in this module that
    needs to know whether the sun is up: `_production_absence_findings`.
    Every other rule here is already correctly silent overnight for reasons
    that have nothing to do with sun position -- the peer-comparison rules
    only compare devices that are actually producing (a whole site at rest
    has no peer above the comparison floor, so nothing is compared), and a
    resting inverter already reads `standby`, not `unavailable`, at the
    classification layer (`diagnostics/rules.py`) before this module ever
    sees it. Passing `None` (the default) skips the new rule entirely rather
    than guessing a window -- exactly what every caller that predates
    coordinates existing keeps doing, unchanged.
    """
    findings: list[DiagnosticFinding] = []
    for row in rows:
        findings.extend(_device_state_findings(row, asset_id=asset_id, now=now, history_for_device=history_for_device, stale_after=stale_after))
    findings.extend(_peer_comparison_findings(rows, asset_id=asset_id))
    findings.extend(_coverage_findings(rows, asset_id=asset_id))
    if production_window is not None:
        findings.extend(
            _production_absence_findings(
                rows, asset_id=asset_id, now=now, window=production_window, history_for_device=history_for_device
            )
        )
    findings.sort(key=lambda finding: (SEVERITY_ORDER.get(finding.severity, 99), finding.device_label or ""))
    return findings


def evaluate_plant_findings(
    *,
    asset_id: int,
    condition: str | None,
    observed_at: datetime | None,
    confirmed_at: datetime | None = None,
    now: datetime,
    stale_after: timedelta = _DEFAULT_STALE_AFTER,
) -> list[DiagnosticFinding]:
    """Findings about the installation as a whole, from its latest plant reading.

    Every other rule in this module judges one inverter. These judge what the
    provider says about the *site*, which is the question an operator is
    actually woken up for -- and the one V1 alerted on and V2 did not, because
    nothing here had ever looked at `monitoring_observations`.

    An asset with no plant reading at all produces nothing. "Never read" is an
    onboarding gap, not an incident: opening one for every unmapped
    installation would bury the sites that genuinely fell over under a list of
    sites nobody ever connected.

    A fresh reading whose condition is `unknown` also produces nothing, on
    purpose. The provider answering without a usable status is already visible
    on the installation badge and the diagnostics page; making it an incident
    would mean a nightly warning for every Sigenergy plant, whose energy-flow
    payload carries no status once the sun is down. "We do not know" is not
    the same claim as "something is wrong", and only the second one is worth
    interrupting someone for.

    `confirmed_at` and `observed_at` are different questions and staleness only
    ever asks the first. `monitoring_observations` is append-only and a new row
    is written **only when the evidence changes** -- a plant that stays
    operational keeps yesterday's row while being re-read every 15 minutes, and
    `MonitoringCurrentState.last_confirmed_at` is what moves. Judging staleness
    by `observed_at` would therefore report every healthy, unchanging plant as
    unread within a day of its last state change, which is the opposite of the
    truth. `observed_at` still answers "since when has it been in this state",
    which is what the outage rules below want for `active_since`.
    """
    if observed_at is None:
        return []
    last_read = confirmed_at or observed_at
    if now - last_read > stale_after:
        hours = int((now - last_read).total_seconds() // 3600)
        return [
            DiagnosticFinding(
                rule_code="plant_state_stale",
                severity="warning",
                asset_id=asset_id,
                summary=f"Sem leitura de estado desta instalação há {hours}h — o estado mostrado já não descreve agora.",
                evidence={"last_condition": condition, "observed_at": observed_at.isoformat(), "confirmed_at": last_read.isoformat(), "stale_after_hours": stale_after.total_seconds() / 3600},
                observed_at=last_read,
                active_since=last_read + stale_after,
                missing_data="Porque parou é uma pergunta sobre a ligação ao provider, não sobre a central -- ver Estado do sistema.",
            )
        ]
    if condition == "offline":
        return [
            DiagnosticFinding(
                rule_code="plant_offline",
                severity="critical",
                asset_id=asset_id,
                summary="A instalação está sem comunicação segundo o provider.",
                evidence={"condition": condition, "observed_at": observed_at.isoformat()},
                observed_at=observed_at,
                active_since=observed_at,
            )
        ]
    if condition == "fault":
        return [
            DiagnosticFinding(
                rule_code="plant_fault",
                severity="critical",
                asset_id=asset_id,
                summary="O provider reporta avaria nesta instalação.",
                evidence={"condition": condition, "observed_at": observed_at.isoformat()},
                observed_at=observed_at,
                active_since=observed_at,
            )
        ]
    if condition == "warning":
        return [
            DiagnosticFinding(
                rule_code="plant_warning",
                severity="warning",
                asset_id=asset_id,
                summary="O provider assinala um aviso nesta instalação.",
                evidence={"condition": condition, "observed_at": observed_at.isoformat()},
                observed_at=observed_at,
                active_since=observed_at,
            )
        ]
    return []


def _device_state_findings(
    row: dict[str, Any],
    *,
    asset_id: int,
    now: datetime,
    history_for_device: Callable[[int], list[DeviceStatusFact]] | None,
    stale_after: timedelta,
) -> list[DiagnosticFinding]:
    device_id = row["device_id"]
    label = row["label"]
    findings: list[DiagnosticFinding] = []

    if not row["has_reading"]:
        findings.append(
            DiagnosticFinding(
                rule_code="device_no_history",
                severity="warning",
                asset_id=asset_id,
                device_id=device_id,
                device_label=label,
                summary=f"{label}: nunca teve uma leitura de estado registada.",
                evidence={"device_kind": row.get("device_kind")},
                observed_at=None,
                active_since=None,
                missing_data="Nenhuma leitura existe para este dispositivo em device_status_facts; "
                "não é possível saber desde quando, apenas que não há evidência nenhuma.",
            )
        )
        return findings  # no other state rule applies without a reading

    availability = row["availability_status"]
    observed_at = row["observed_at"]

    if availability == "unavailable":
        since = _active_since(device_id, history_for_device, predicate=lambda fact: fact.availability_status == "unavailable")
        findings.append(
            DiagnosticFinding(
                rule_code="device_unavailable",
                severity="critical",
                asset_id=asset_id,
                device_id=device_id,
                device_label=label,
                summary=f"{label}: indisponível na última leitura.",
                evidence={"availability_status": availability, "active_power_kw": _num(row.get("active_power_kw"))},
                observed_at=observed_at,
                active_since=since or observed_at,
                missing_data=None if history_for_device else "Histórico do dispositivo não consultado; 'desde' é apenas a última leitura.",
            )
        )
    elif availability == "unknown":
        since = _active_since(device_id, history_for_device, predicate=lambda fact: fact.availability_status == "unknown")
        findings.append(
            DiagnosticFinding(
                rule_code="device_unknown_status",
                severity="warning",
                asset_id=asset_id,
                device_id=device_id,
                device_label=label,
                summary=f"{label}: estado desconhecido na última leitura (código de estado não reconhecido pelo provider).",
                evidence={"availability_status": availability},
                observed_at=observed_at,
                active_since=since or observed_at,
                missing_data=None if history_for_device else "Histórico do dispositivo não consultado; 'desde' é apenas a última leitura.",
            )
        )

    if observed_at is not None and now - observed_at > stale_after:
        findings.append(
            DiagnosticFinding(
                rule_code="stale_reading",
                severity="warning",
                asset_id=asset_id,
                device_id=device_id,
                device_label=label,
                summary=f"{label}: última leitura há mais de {int(stale_after.total_seconds() // 3600)}h.",
                evidence={"observed_at": observed_at.isoformat(), "age_hours": round((now - observed_at).total_seconds() / 3600, 1)},
                observed_at=observed_at,
                active_since=observed_at,
                missing_data="Idade calculada apenas contra a última leitura conhecida; "
                "não indica se o provider está a falhar ou se este dispositivo simplesmente não é sondado com mais frequência.",
            )
        )

    return findings


def _active_since(
    device_id: int,
    history_for_device: Callable[[int], list[DeviceStatusFact]] | None,
    *,
    predicate: Callable[[DeviceStatusFact], bool],
) -> datetime | None:
    if history_for_device is None:
        return None
    history = history_for_device(device_id)  # newest first, per DeviceStatusRepository.history_for_device
    since: datetime | None = None
    for fact in history:
        if not predicate(fact):
            break
        since = fact.observed_at
    return since


def _peer_comparison_findings(rows: list[dict[str, Any]], *, asset_id: int) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["has_reading"] and row.get("active_power_kw") is not None and row.get("observed_at") is not None:
            by_kind.setdefault(row["device_kind"], []).append(row)

    for kind, peers in by_kind.items():
        if len(peers) < 2:
            continue
        for row in peers:
            comparable = [
                other
                for other in peers
                if other["device_id"] != row["device_id"]
                and abs(other["observed_at"] - row["observed_at"]) <= _COMPARISON_TOLERANCE
            ]
            if not comparable:
                continue
            peer_max = max(_num(other["active_power_kw"]) for other in comparable)
            own_power = _num(row["active_power_kw"])
            if peer_max < _DISPARITY_MIN_PEER_POWER_KW:
                continue
            if own_power == Decimal("0") and peer_max > _DISPARITY_MIN_PEER_POWER_KW:
                findings.append(
                    DiagnosticFinding(
                        rule_code="zero_power_while_peers_active",
                        severity="critical",
                        asset_id=asset_id,
                        device_id=row["device_id"],
                        device_label=row["label"],
                        summary=f"{row['label']}: potência zero enquanto dispositivos comparáveis ({kind}) produzem até {peer_max} kW.",
                        evidence={"own_active_power_kw": str(own_power), "peer_max_active_power_kw": str(peer_max), "device_kind": kind},
                        observed_at=row["observed_at"],
                        active_since=row["observed_at"],
                        fact_ids=(),
                        missing_data="Comparação apenas na leitura mais recente; histórico da disparidade não reconstituído retroativamente.",
                    )
                )
            elif own_power > 0 and own_power / peer_max < _DISPARITY_RATIO_THRESHOLD:
                findings.append(
                    DiagnosticFinding(
                        rule_code="power_disparity_among_peers",
                        severity="warning",
                        asset_id=asset_id,
                        device_id=row["device_id"],
                        device_label=row["label"],
                        summary=f"{row['label']}: potência ({own_power} kW) muito abaixo de um dispositivo comparável ({kind}, até {peer_max} kW).",
                        evidence={"own_active_power_kw": str(own_power), "peer_max_active_power_kw": str(peer_max), "ratio": str(round(own_power / peer_max, 3)), "device_kind": kind},
                        observed_at=row["observed_at"],
                        active_since=row["observed_at"],
                        fact_ids=(),
                        missing_data="Comparação apenas na leitura mais recente; não ajustado por potência nominal por falta de peso fiável quando não configurado.",
                    )
                )

        # Same shape, for day_energy_kwh -- a persistent gap here is a
        # stronger signal than an instantaneous power gap (integrates the
        # whole day, not one moment's cloud shadow), so it stays a distinct
        # rule rather than folded into the power one.
        energy_peers = [row for row in peers if row.get("day_energy_kwh") is not None]
        for row in energy_peers:
            comparable = [
                other
                for other in energy_peers
                if other["device_id"] != row["device_id"]
                and abs(other["observed_at"] - row["observed_at"]) <= _COMPARISON_TOLERANCE
            ]
            if not comparable:
                continue
            peer_max_energy = max(_num(other["day_energy_kwh"]) for other in comparable)
            own_energy = _num(row["day_energy_kwh"])
            if peer_max_energy < Decimal("1"):
                continue  # too early in the day for an energy gap to mean anything
            if own_energy / peer_max_energy < _DISPARITY_RATIO_THRESHOLD:
                findings.append(
                    DiagnosticFinding(
                        rule_code="daily_energy_disparity_among_peers",
                        severity="warning",
                        asset_id=asset_id,
                        device_id=row["device_id"],
                        device_label=row["label"],
                        summary=f"{row['label']}: energia do dia ({own_energy} kWh) muito abaixo de um dispositivo comparável ({kind}, até {peer_max_energy} kWh).",
                        evidence={"own_day_energy_kwh": str(own_energy), "peer_max_day_energy_kwh": str(peer_max_energy), "ratio": str(round(own_energy / peer_max_energy, 3)), "device_kind": kind},
                        observed_at=row["observed_at"],
                        active_since=row["observed_at"],
                        fact_ids=(),
                        missing_data="Comparação apenas na leitura mais recente; um desvio cedo no dia pode ainda equilibrar-se.",
                    )
                )
    return findings


# How long every device with real evidence has to have read exactly zero
# power, together, inside a productive window, before this counts as a total
# production failure rather than a cloud passing over. The one number in
# GOAL.md's own policy table this module did not yet have a rule for
# ("Falha total de produção > 30 min em período produtivo | cria").
_PRODUCTION_ABSENCE_THRESHOLD = timedelta(minutes=30)


def _production_absence_findings(
    rows: list[dict[str, Any]],
    *,
    asset_id: int,
    now: datetime,
    window: ProductionWindow,
    history_for_device: Callable[[int], list[DeviceStatusFact]] | None,
) -> list[DiagnosticFinding]:
    """Every device with real evidence reads zero power, during daylight.

    Deliberately narrow. It fires only when there is positive evidence of
    zero -- a real reading whose `active_power_kw` is exactly zero -- never
    from silence: an asset with no fresh reading at all is `device_no_history`
    or `stale_reading`'s problem, a different and already-covered one, and
    conflating "no data" with "zero power" would invent a claim the data does
    not support. It also fires only when `window.is_productive`: at night,
    at dawn inside the margin, or for an installation with no coordinates
    (`window.is_known` is `False`), zero is the expected reading and this
    rule is silent by construction rather than by a special case.

    Assumes every row here is a production-capable device -- true today,
    since `Device.device_kind` in this fleet is `inverter` on every row a
    provider has ever populated. A meter row (day energy consumed, not
    produced) would need excluding by kind once one exists; there are none
    to exclude yet.
    """
    if not window.is_productive:
        return []
    readings = [row for row in rows if row["has_reading"] and row.get("active_power_kw") is not None]
    if not readings:
        return []
    if any(_num(row["active_power_kw"]) > 0 for row in readings):
        return []

    since_per_device: list[datetime] = []
    for row in readings:
        since = _active_since(
            row["device_id"],
            history_for_device,
            predicate=lambda fact: fact.active_power_kw is not None and fact.active_power_kw <= 0,
        )
        since_per_device.append(since or row["observed_at"])
    # The joint "every device is at zero" condition can only have started once
    # the *last* device to still be producing also dropped to zero -- the
    # latest of the per-device zero-since points, not the earliest.
    since = max(since_per_device)
    if now - since < _PRODUCTION_ABSENCE_THRESHOLD:
        return []

    return [
        DiagnosticFinding(
            rule_code="zero_production_in_productive_window",
            severity="critical",
            asset_id=asset_id,
            summary=f"Produção nula em todos os {len(readings)} equipamentos com leitura, há mais de "
            f"{int(_PRODUCTION_ABSENCE_THRESHOLD.total_seconds() // 60)} min, dentro do período produtivo.",
            evidence={
                "devices_with_zero_reading": len(readings),
                "since": since.isoformat(),
                "window_starts_at": window.starts_at.isoformat() if window.starts_at else None,
                "window_ends_at": window.ends_at.isoformat() if window.ends_at else None,
            },
            observed_at=now,
            active_since=since,
            missing_data=None if history_for_device else "Histórico não consultado; 'desde' é apenas a última leitura de cada equipamento.",
        )
    ]


def _coverage_findings(rows: list[dict[str, Any]], *, asset_id: int) -> list[DiagnosticFinding]:
    if not rows:
        return []
    with_reading = sum(1 for row in rows if row["has_reading"])
    if 0 < with_reading < len(rows):
        return [
            DiagnosticFinding(
                rule_code="partial_device_coverage",
                severity="info",
                asset_id=asset_id,
                summary=f"Cobertura parcial: {with_reading} de {len(rows)} dispositivos têm alguma leitura.",
                evidence={"devices_with_reading": with_reading, "devices_total": len(rows)},
                missing_data="Nível de finding do asset, não de um dispositivo específico -- ver 'device_no_history' para quais.",
            )
        ]
    return []


def _num(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))
