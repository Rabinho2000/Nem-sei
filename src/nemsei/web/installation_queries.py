"""The operational read-model: what the Installation-first UI actually reads.

`Asset` stays the query anchor, not `Installation`, for a concrete reason:
every fact table (`production_facts`, `device_status_facts`,
`diagnostic_incidents`, `asset_provider_mappings`) is keyed by `asset_id`,
and in production today `installations` is empty -- the backfill
(`installations.service.backfill_installations_from_assets`) is built and
tested but has not been deployed. Every field this module reads through
`Installation` therefore degrades to `None`/an honest "not yet available"
state rather than making the whole page empty, so the list and detail pages
work today and pick up Installation-level data (coordinates, the productive
window, work orders) the moment the backfill runs, without a code change.

This module never invents an operational-priority ranking. `web.
operational_priority.installation_priority` is the one adapter seam for
that; another context is building the real, shared `operational_priority`
engine for Telegram and the morning briefing, and this module's job is to
feed it the same counts, never to re-rank on its own logic.

Incident counts are always split fault / communication / coverage
(`diagnostics.incident_categories`) and never merged back into one number --
GOAL.md is explicit that a monitoring gap must not present as a real fault,
and a merged count is exactly how that would happen silently.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device, Organization
from nemsei.contracts.priority import describe as describe_commercial
from nemsei.contracts.service import esco_status_map, om_status_map
from nemsei.diagnostics.incident_categories import CATEGORY_LABELS, CATEGORY_TONES, INCIDENT_CATEGORIES, incident_category
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.models import Installation
from nemsei.notifications.impact import estimate_energy_impact, estimate_financial_impact
from nemsei.monitoring.installation_state import current_installation_states
from nemsei.monitoring.production_window import window_for
from nemsei.reporting.commercial import (
    billing_config_from,
    confirmed_financial_model,
    report_type_for,
    resolve_billing_config,
    resolve_tariff,
)
from nemsei.reporting.models import FinancialModelMonth
from nemsei.reporting.rules.billing import calculate_billing
from nemsei.reporting.rules.types import EnergyBreakdown, ReportType
from nemsei.shared.clock import utc_now
from nemsei.timeline.service import installation_timeline
from nemsei.web.operational_priority import installation_priority
from nemsei.web.queries import list_assets_data
from nemsei.web.series import energy_balance, headline, production_consumption_series
from nemsei.web.work_order_queries import overdue_and_unscheduled_counts
from nemsei.work_orders.service import work_orders_for_installation


def incident_counts_by_category(session: Session, *, asset_ids: list[int]) -> dict[int, dict[str, Any]]:
    """`{fault, communication, coverage}` open-incident counts per asset, in
    one query -- a list page must not run one query per row."""
    empty = {category: 0 for category in INCIDENT_CATEGORIES}
    if not asset_ids:
        return {}
    rows = session.execute(
        select(DiagnosticIncident.asset_id, DiagnosticIncident.rule_code, func.count(DiagnosticIncident.id))
        .where(DiagnosticIncident.asset_id.in_(asset_ids), DiagnosticIncident.status == "open")
        .group_by(DiagnosticIncident.asset_id, DiagnosticIncident.rule_code)
    ).all()
    counts: dict[int, dict[str, int]] = {asset_id: dict(empty) for asset_id in asset_ids}
    for asset_id, rule_code, count in rows:
        counts[asset_id][incident_category(rule_code)] += int(count)
    return {
        asset_id: {
            **values,
            "total": sum(values.values()),
            "labels": CATEGORY_LABELS,
            "tones": CATEGORY_TONES,
        }
        for asset_id, values in counts.items()
    }


def installation_list_rows(
    session: Session,
    *,
    search: str = "",
    needs_review: str = "",
    provider: str = "",
    mapping: str = "",
    om: str = "todos",
    family: str = "",
    page_value: str | None = None,
    per_page: int = 25,
) -> dict[str, Any]:
    """The operational list: `queries.list_assets_data`'s rows, enriched with
    incident category counts, ESCO status, open work orders, and a priority
    hint -- never a second copy of the identity/mapping fields that function
    already computes and already has tests for.

    `om` defaults to `"todos"` here, the opposite default from
    `list_assets_data` itself. The admin `/assets` screen defaults to the
    O&M-scoped fleet because it is a mapping/identity tool where the O&M
    parque is the common case to edit; "Instalações" is the operational
    directory GOAL.md asks for and has to show the whole fleet by default,
    with O&M scope as an explicit filter rather than a hidden one.
    """
    base = list_assets_data(
        session, search=search, needs_review=needs_review, provider=provider,
        mapping=mapping, om=om, family=family, page_value=page_value, per_page=per_page,
    )
    asset_ids = [row["id"] for row in base["assets"]]
    incidents = incident_counts_by_category(session, asset_ids=asset_ids)
    esco_states = esco_status_map(session, asset_ids=asset_ids)
    work_counts = overdue_and_unscheduled_counts(session, asset_ids=asset_ids)

    for row in base["assets"]:
        asset_id = row["id"]
        row_incidents = incidents.get(asset_id, {category: 0 for category in INCIDENT_CATEGORIES})
        row["incidents"] = row_incidents
        row["esco"] = esco_states.get(asset_id, {"status": "none"})
        row["work"] = work_counts.get(asset_id, {"overdue": 0, "unscheduled": 0})
        priority = installation_priority(
            real_fault_count=row_incidents.get("fault", 0),
            communication_count=row_incidents.get("communication", 0),
            commercial_priority=row["commercial"]["priority"],
        )
        row["priority_rank"] = priority.rank
        row["priority_reason"] = priority.reason

    return base


def _installation_header(session: Session, asset: Asset, organization_name: str | None) -> dict[str, Any]:
    installation = session.get(Installation, asset.installation_id) if asset.installation_id else None
    latitude = installation.latitude if installation else None
    longitude = installation.longitude if installation else None
    om_state = om_status_map(session, asset_ids=[asset.id])[asset.id]
    esco_state = esco_status_map(session, asset_ids=[asset.id])[asset.id]
    commercial = describe_commercial(asset.contract_type, om_state["status"])
    state = current_installation_states(session, asset_ids=[asset.id])[asset.id]
    return {
        "asset": asset,
        "installation": installation,
        # Explicit, not inferred from `installation is None`: a template
        # must be able to say *why* a feature is unavailable (backfill not
        # yet deployed) rather than just hiding it.
        "has_installation": installation is not None,
        "organization_name": organization_name,
        "locality": (installation.locality if installation else asset.locality) or None,
        "power_kwp": asset.installed_dc_power_kw,
        "state": state,
        "om": om_state,
        "esco": esco_state,
        "commercial": commercial,
        "coordinates": (latitude, longitude),
        "coordinates_known": latitude is not None and longitude is not None,
        # `unknown` for every installation until coordinates are imported --
        # a true state, not a bug, shown honestly rather than guessed.
        "production_window": window_for(latitude=latitude, longitude=longitude, at=utc_now()),
    }


def _resumo_tab(session: Session, asset: Asset, *, period: str) -> dict[str, Any]:
    counts = incident_counts_by_category(session, asset_ids=[asset.id])[asset.id]
    return {
        "headline": headline(session, asset_id=asset.id),
        # Not "chart" -- the template imports web/templates/macros/chart.html
        # as `chart`, and a context key of the same name would shadow it.
        "production_chart": production_consumption_series(session, asset_id=asset.id, period=period),
        "incidents": counts,
        "recent_timeline": installation_timeline(session, installation_id=asset.installation_id)[-5:]
        if asset.installation_id
        else [],
    }


def _incident_impact(
    session: Session, incident: DiagnosticIncident, *, latitude, longitude, billing_config
) -> dict[str, Any] | None:
    """Estimated lost production/revenue for one open incident, via the
    shared engine (`notifications/impact.py`) another context built for
    Telegram's episode alerts -- the exact GOAL.md example ("Inversor 2
    indisponível durante 17h... Produção perdida estimada...") reaches the
    UI through the same calculation Telegram uses, never a second one.
    Only for `fault`/`communication` incidents: a `coverage` incident (a
    stale reading, a device that never reported) has no honest basis to
    estimate lost production from -- `estimate_energy_impact` already
    refuses to guess without a real recent power reading, but asking it at
    all for "we simply do not know the device's state" would imply a
    confidence this category is defined by not having.
    """
    if incident_category(incident.rule_code) == "coverage":
        return None
    energy = estimate_energy_impact(
        session, asset_id=incident.asset_id, device_id=incident.device_id,
        start=incident.opened_at, end=utc_now(), latitude=latitude, longitude=longitude,
    )
    if billing_config is None:
        return {"energy": energy, "savings": None, "solcor_revenue": None}
    savings = estimate_financial_impact(energy=energy, price_eur_per_kwh=billing_config.default_electricity_price)
    solcor_revenue = (
        estimate_financial_impact(energy=energy, price_eur_per_kwh=billing_config.solcor_price_per_kwh)
        if billing_config.report_type == "esco"
        else None
    )
    return {"energy": energy, "savings": savings, "solcor_revenue": solcor_revenue}


def _operacao_tab(session: Session, asset: Asset) -> dict[str, Any]:
    open_incidents = list(
        session.scalars(
            select(DiagnosticIncident)
            .where(DiagnosticIncident.asset_id == asset.id, DiagnosticIncident.status == "open")
            .order_by(DiagnosticIncident.severity, DiagnosticIncident.opened_at)
        )
    )
    by_category: dict[str, list[DiagnosticIncident]] = {category: [] for category in INCIDENT_CATEGORIES}
    for incident in open_incidents:
        by_category[incident_category(incident.rule_code)].append(incident)

    installation = session.get(Installation, asset.installation_id) if asset.installation_id else None
    latitude = installation.latitude if installation else None
    longitude = installation.longitude if installation else None
    billing_config = resolve_billing_config(session, asset_id=asset.id, on=utc_now().date())
    impact_by_incident = {
        incident.id: _incident_impact(session, incident, latitude=latitude, longitude=longitude, billing_config=billing_config)
        for incident in open_incidents
    }

    return {
        "incidents_by_category": by_category,
        "category_labels": CATEGORY_LABELS,
        "category_tones": CATEGORY_TONES,
        "total_open": len(open_incidents),
        "impact_by_incident": impact_by_incident,
    }


def _trabalhos_tab(session: Session, asset: Asset) -> dict[str, Any]:
    if asset.installation_id is None:
        return {"blocked": True, "work_orders": []}
    return {"blocked": False, "work_orders": work_orders_for_installation(session, installation_id=asset.installation_id)}


def _equipamentos_tab(session: Session, asset: Asset) -> dict[str, Any]:
    devices = list(session.scalars(select(Device).where(Device.asset_id == asset.id).order_by(Device.id)))
    return {
        "devices": devices,
        "inverters": [device for device in devices if device.device_kind == "inverter"],
        "meters": [device for device in devices if device.device_kind == "meter"],
        # ModuleGroup does not exist yet -- GOAL.md asks for it, it has not
        # been built. Shown as a named, honest gap rather than omitted.
        "module_groups_available": False,
    }


def _expected_vs_actual(session: Session, *, asset: Asset, on: date, actual_kwh: Decimal | None) -> dict[str, Any]:
    """This calendar month's confirmed-model expectation against what was
    actually measured -- reusing `FinancialModel`/`FinancialModelMonth`
    exactly as `reporting.datasets.build_dataset` does, never re-deriving
    "confirmed model" (`reporting.commercial.confirmed_financial_model`) or
    the expected-production lookup a second way.
    """
    model = confirmed_financial_model(session, asset_id=asset.id)
    if model is None:
        return {"available": False, "reason": "no_confirmed_model"}
    expected_row = session.scalar(
        select(FinancialModelMonth).where(
            FinancialModelMonth.financial_model_id == model.id, FinancialModelMonth.month == on.month
        )
    )
    expected_kwh = expected_row.expected_production_kwh if expected_row else None
    if expected_kwh is None:
        return {"available": False, "reason": "no_expected_value_for_month", "model_version": model.version}
    if actual_kwh is None:
        return {
            "available": True, "expected_kwh": expected_kwh, "actual_kwh": None, "deviation_pct": None,
            "model_version": model.version,
        }
    deviation_pct = ((actual_kwh - expected_kwh) / expected_kwh * 100) if expected_kwh else None
    return {
        "available": True,
        "expected_kwh": expected_kwh,
        "actual_kwh": actual_kwh,
        "deviation_pct": deviation_pct,
        "model_version": model.version,
    }


def _performance_tab(session: Session, asset: Asset) -> dict[str, Any]:
    today = utc_now().date()
    month_start = date(today.year, today.month, 1)
    balance = energy_balance(session, asset_id=asset.id, start=month_start, end=today + timedelta(days=1))
    metrics = balance["metrics"]
    actual_kwh = Decimal(str(metrics["production_energy"])) if metrics.get("production_energy") else None
    result: dict[str, Any] = {
        "metrics": metrics,
        "has_production_data": bool(metrics.get("production_energy")),
        "report_type": report_type_for(asset).value,
        "billing": None,
        "expected": _expected_vs_actual(session, asset=asset, on=today, actual_kwh=actual_kwh),
        # Impacto económico das falhas: agora real, por incidente aberto --
        # ver o separador Operação. `notifications/impact.py` (o motor
        # partilhado com o Telegram/resumo matinal) aterrou a meio desta
        # sessão; este separador não repete o cálculo, só aponta para onde
        # ele já aparece, para nunca haver dois números para o mesmo
        # incidente em duas tabs.
        "impact_available": True,
    }
    if not result["has_production_data"]:
        return result
    billing_config = resolve_billing_config(session, asset_id=asset.id, on=today)
    tariff = resolve_tariff(session, asset_id=asset.id, on=today)
    if billing_config is None:
        result["missing"] = "billing_config"
        return result
    breakdown = EnergyBreakdown(
        production_kwh=Decimal(str(metrics["production_energy"])),
        self_use_kwh=Decimal(str(metrics["self_use_energy"])),
        export_kwh=Decimal(str(metrics["export_energy"])),
        consumption_kwh=Decimal(str(metrics["consumption_energy"])),
    )
    result["billing"] = calculate_billing(breakdown, billing_config_from(billing_config))
    result["is_esco"] = report_type_for(asset) == ReportType.ESCO
    result["has_tariff"] = tariff is not None
    return result


TABS = ("resumo", "operacao", "trabalhos", "equipamentos", "performance", "timeline")


def installation_detail(session: Session, *, asset_id: int, tab: str = "resumo", period: str = "week") -> dict[str, Any] | None:
    """Everything one tab of the installation detail page needs.

    Only the active tab's data is computed -- six tabs' worth of queries on
    every request would make the page slow for no reason nobody is looking
    at five of them at once.
    """
    if tab not in TABS:
        tab = "resumo"
    row = session.execute(
        select(Asset, Organization.display_name)
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .where(Asset.id == asset_id)
    ).first()
    if row is None:
        return None
    asset, organization_name = row

    header = _installation_header(session, asset, organization_name)
    tab_data: dict[str, Any] = {}
    if tab == "resumo":
        tab_data = _resumo_tab(session, asset, period=period)
    elif tab == "operacao":
        tab_data = _operacao_tab(session, asset)
    elif tab == "trabalhos":
        tab_data = _trabalhos_tab(session, asset)
    elif tab == "equipamentos":
        tab_data = _equipamentos_tab(session, asset)
    elif tab == "performance":
        tab_data = _performance_tab(session, asset)
    elif tab == "timeline":
        tab_data = {
            "events": installation_timeline(session, installation_id=asset.installation_id)
            if asset.installation_id
            else [],
            "blocked": asset.installation_id is None,
        }

    return {
        **header,
        "tab": tab,
        "tabs": TABS,
        "period": period,
        **tab_data,
    }
