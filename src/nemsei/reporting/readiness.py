"""What can be reported this month, what cannot, and which of the two it is.

The reporting screens used to answer one question -- "which reports exist?" --
and an operator standing in front of 267 installations on the last day of
August needs the other one: *what can I produce, what is missing, and what do I
do next?* That is three different absences, and lumping them together is what
made the old screen useless:

  * no energy for the month at all. Nothing to report; go and fetch data.
  * energy, but not a closed month. A real report, marked provisional, stating
    no euros.
  * energy and a closed month, but no commercial configuration. The energy
    report is fine; the financial one cannot be produced and must not be
    invented.

One pass over the fleet answers all three, in two grouped queries rather than
267 round trips, because this renders on every page load.

Reads persisted rows only. Nothing here can reach a provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable

from sqlalchemy import Date, cast, distinct, func, or_, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.reporting.commercial_models import AssetBillingConfig, AssetTariff
from nemsei.reporting.models import ReportingDataset, ReportSnapshot


#: Ordered worst-first, which is also how an operator wants the list sorted.
READINESS_STATES = ("blocked", "needs_commercial", "provisional", "final")

STATE_LABELS_PT = {
    "blocked": "Sem dados",
    "needs_commercial": "Falta configuração",
    "provisional": "Provisório",
    "final": "Final",
}


@dataclass(frozen=True)
class AssetReadiness:
    """One installation's answer for one month."""

    asset_id: int
    name: str
    owner_name: str | None
    contract_type: str | None
    is_esco: bool
    provider_code: str | None
    external_id: str | None
    observed_days: int
    expected_days: int
    has_energy: bool
    has_commercial: bool
    has_tariff: bool
    metrics_present: tuple[str, ...]
    snapshot_id: int | None
    snapshot_state: str | None
    snapshot_created_at: datetime | None

    @property
    def has_report(self) -> bool:
        return self.snapshot_id is not None

    @property
    def missing_days(self) -> int:
        return max(self.expected_days - self.observed_days, 0)

    @property
    def coverage_pct(self) -> float:
        if not self.expected_days:
            return 0.0
        return self.observed_days / self.expected_days * 100.0

    @property
    def can_report_money(self) -> bool:
        """Whether a financial figure is even possible for this installation.

        Two things are needed and neither substitutes for the other: the rates,
        and a self-consumption figure to apply them to. A FusionSolar
        installation has the second only if something else supplied it, because
        the verified contract exposes production alone.
        """
        return self.has_commercial and "self_use" in self.metrics_present

    @property
    def state(self) -> str:
        if not self.has_energy:
            return "blocked"
        if self.is_esco and not self.has_commercial:
            return "needs_commercial"
        if self.snapshot_state == "final":
            return "final"
        return "provisional"

    @property
    def state_label(self) -> str:
        return STATE_LABELS_PT[self.state]

    @property
    def blockers(self) -> tuple[str, ...]:
        """What to fix, in the operator's words, worst first."""
        reasons: list[str] = []
        if not self.has_energy:
            reasons.append("Sem energia registada para o mês")
        elif self.missing_days:
            reasons.append(f"Faltam {self.missing_days} de {self.expected_days} dias")
        if self.is_esco and not self.has_commercial:
            reasons.append("Sem taxas de venda / poupança / excedente")
        if self.has_commercial and "self_use" not in self.metrics_present:
            reasons.append("Sem autoconsumo: sem valores em euros")
        # Distinct from the billing-config gap above: `AssetTariff` feeds the
        # tariff summary/breakdown a report shows, not the savings/export/
        # Solcor-payment euro figures themselves (those come from
        # `AssetBillingConfig` alone -- see `can_report_money`). A report can
        # be financially final and still be missing this.
        if self.is_esco and self.has_commercial and not self.has_tariff:
            reasons.append("Sem tarifa em vigor para o mês")
        return tuple(reasons)


#: `production_energy` decides whether a month can be reported at all; the rest
#: decide whether it can be reported financially.
_METRIC_NAMES = {
    "production_energy": "production",
    "self_use_energy": "self_use",
    "export_energy": "export",
    "consumption_energy": "consumption",
    "grid_import_energy": "grid_import",
}


def _as_datetime(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def month_bounds(month: str) -> tuple[date, date]:
    """`"2026-08"` as the half-open period the dataset layer uses."""
    start = datetime.strptime(month, "%Y-%m").date()
    return start, date(start.year + (start.month == 12), start.month % 12 + 1, 1)


def _coverage(session: Session, start: date, end: date) -> dict[int, dict[str, Any]]:
    """Distinct measured days and metrics per asset, in one grouped query.

    Only the current revision of a fact counts, the same rule the dataset layer
    applies, or a corrected day would be counted twice and a month would look
    better covered than it is. A row persisted as explicitly missing does not
    count as a day, which is the missing-is-not-zero rule reaching coverage.
    """
    current = (
        select(
            ProductionFact.asset_id,
            ProductionFact.metric_kind,
            ProductionFact.period_start,
            ProductionFact.value,
            ProductionFact.quality,
        )
        .distinct(ProductionFact.provider_mapping_id, ProductionFact.source_fact_key)
        .where(
            ProductionFact.period_start >= _as_datetime(start),
            ProductionFact.period_start < _as_datetime(end),
            ProductionFact.granularity == "day",
        )
        .order_by(
            ProductionFact.provider_mapping_id,
            ProductionFact.source_fact_key,
            ProductionFact.source_revision.desc(),
        )
        .subquery()
    )
    rows = session.execute(
        select(
            current.c.asset_id,
            current.c.metric_kind,
            func.count(distinct(cast(current.c.period_start, Date))),
        )
        .where(current.c.value.is_not(None), current.c.quality != "missing")
        .group_by(current.c.asset_id, current.c.metric_kind)
    ).all()

    coverage: dict[int, dict[str, Any]] = {}
    for asset_id, metric_kind, days in rows:
        entry = coverage.setdefault(asset_id, {"days": 0, "metrics": set()})
        name = _METRIC_NAMES.get(metric_kind)
        if name is None:
            continue
        entry["metrics"].add(name)
        if metric_kind == "production_energy":
            entry["days"] = days
    return coverage


def _latest_snapshots(session: Session, start: date, end: date) -> dict[int, ReportSnapshot]:
    rows = session.execute(
        select(ReportSnapshot, ReportingDataset.asset_id)
        .join(ReportingDataset, ReportingDataset.id == ReportSnapshot.dataset_id)
        .where(
            ReportingDataset.scope == "asset",
            ReportingDataset.period_start == start,
            ReportingDataset.period_end == end,
        )
        .order_by(ReportSnapshot.created_at.desc(), ReportSnapshot.id.desc())
    ).all()
    latest: dict[int, ReportSnapshot] = {}
    for snapshot, asset_id in rows:
        latest.setdefault(asset_id, snapshot)
    return latest


def _snapshot_state(snapshot: ReportSnapshot) -> str:
    payload = snapshot.payload_json or {}
    state = payload.get("reporting_state")
    if isinstance(state, str):
        return state
    return "final" if payload.get("production_is_final") else "provisional"


def _billing_by_asset(session: Session, on: date) -> dict[int, AssetBillingConfig]:
    rows = session.scalars(
        select(AssetBillingConfig).where(
            AssetBillingConfig.valid_from <= on,
            or_(AssetBillingConfig.valid_to.is_(None), AssetBillingConfig.valid_to > on),
        )
    ).all()
    return {row.asset_id: row for row in rows}


def _tariff_by_asset(session: Session, on: date) -> dict[int, AssetTariff]:
    rows = session.scalars(
        select(AssetTariff).where(
            AssetTariff.valid_from <= on,
            or_(AssetTariff.valid_to.is_(None), AssetTariff.valid_to > on),
        )
    ).all()
    return {row.asset_id: row for row in rows}


def _plant_claims(session: Session) -> dict[int, tuple[str | None, str | None]]:
    rows = session.execute(
        select(AssetProviderMapping.asset_id, ProviderConnection.provider_code, AssetProviderMapping.external_id)
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .where(
            AssetProviderMapping.resource_kind == "plant",
            AssetProviderMapping.mapping_status == "active",
        )
        .order_by(AssetProviderMapping.asset_id, AssetProviderMapping.id)
    ).all()
    claims: dict[int, tuple[str | None, str | None]] = {}
    for asset_id, provider_code, external_id in rows:
        claims.setdefault(asset_id, (provider_code, external_id))
    return claims


def _is_esco(asset: Asset, billing: AssetBillingConfig | None) -> bool:
    """The configuration wins where it exists; otherwise the contract text.

    Deliberately not `detect_report_type`, which answers EPC both when it reads
    "EPC" and when it reads nothing at all. A screen that has to say how many
    ESCOs there are may not count the unstated ones as EPC by accident.
    """
    if billing is not None:
        return billing.report_type == "esco"
    return "esco" in (asset.contract_type or "").strip().casefold()


def fleet_readiness(session: Session, *, month: str) -> list[AssetReadiness]:
    """Every installation's answer for one month, ESCO first, worst first."""
    start, end = month_bounds(month)
    expected_days = (end - start).days

    coverage = _coverage(session, start, end)
    snapshots = _latest_snapshots(session, start, end)
    billing = _billing_by_asset(session, start)
    tariffs = _tariff_by_asset(session, start)
    claims = _plant_claims(session)

    owners = {
        row.id: row.display_name
        for row in session.scalars(select(Organization)).all()
    }

    readiness: list[AssetReadiness] = []
    for asset in session.scalars(select(Asset).order_by(Asset.canonical_name)).all():
        entry = coverage.get(asset.id, {"days": 0, "metrics": set()})
        snapshot = snapshots.get(asset.id)
        config = billing.get(asset.id)
        provider_code, external_id = claims.get(asset.id, (None, None))
        readiness.append(
            AssetReadiness(
                asset_id=asset.id,
                name=asset.canonical_name,
                owner_name=owners.get(asset.owner_id) if asset.owner_id else None,
                contract_type=asset.contract_type,
                is_esco=_is_esco(asset, config),
                provider_code=provider_code,
                external_id=external_id,
                observed_days=int(entry["days"]),
                expected_days=expected_days,
                has_energy=int(entry["days"]) > 0,
                has_commercial=config is not None,
                has_tariff=asset.id in tariffs,
                metrics_present=tuple(sorted(entry["metrics"])),
                snapshot_id=snapshot.id if snapshot else None,
                snapshot_state=_snapshot_state(snapshot) if snapshot else None,
                snapshot_created_at=snapshot.created_at if snapshot else None,
            )
        )
    return sort_for_operator(readiness)


def sort_for_operator(rows: Iterable[AssetReadiness]) -> list[AssetReadiness]:
    """ESCO before EPC, then worst state first, then by name.

    ESCO first because that is where the revenue is and because those are the
    reports that need commercial configuration nobody has entered yet. Worst
    first because a screen sorted alphabetically hides exactly the rows an
    operator opened it to find.
    """
    return sorted(
        rows,
        key=lambda row: (not row.is_esco, READINESS_STATES.index(row.state), row.name.casefold()),
    )


def summarise(rows: Iterable[AssetReadiness]) -> dict[str, Any]:
    """The counts the landing page leads with."""
    rows = list(rows)
    esco = [row for row in rows if row.is_esco]
    return {
        "total": len(rows),
        "esco": len(esco),
        "epc": len(rows) - len(esco),
        "final": sum(1 for row in rows if row.state == "final"),
        "provisional": sum(1 for row in rows if row.state == "provisional"),
        "blocked": sum(1 for row in rows if row.state == "blocked"),
        "needs_commercial": sum(1 for row in rows if row.state == "needs_commercial"),
        "esco_needs_commercial": sum(1 for row in esco if not row.has_commercial),
        "esco_needs_tariff": sum(1 for row in esco if row.has_commercial and not row.has_tariff),
        "without_energy": sum(1 for row in rows if not row.has_energy),
        "generated": sum(1 for row in rows if row.has_report),
        "not_generated": sum(1 for row in rows if not row.has_report),
        "reportable": sum(1 for row in rows if row.has_energy),
        "money_possible": sum(1 for row in rows if row.can_report_money),
    }


def filter_readiness(
    rows: Iterable[AssetReadiness],
    *,
    contract: str = "",
    state: str = "",
    generated: str = "",
    search: str = "",
) -> list[AssetReadiness]:
    """Narrow the fleet by the questions the screen actually asks."""
    result = list(rows)
    if contract == "esco":
        result = [row for row in result if row.is_esco]
    elif contract == "epc":
        result = [row for row in result if not row.is_esco]
    if state in READINESS_STATES:
        result = [row for row in result if row.state == state]
    if generated == "yes":
        result = [row for row in result if row.has_report]
    elif generated == "no":
        result = [row for row in result if not row.has_report]
    needle = search.strip().casefold()
    if needle:
        result = [
            row
            for row in result
            if needle in row.name.casefold()
            or needle in (row.owner_name or "").casefold()
            or needle in (row.external_id or "").casefold()
        ]
    return result
