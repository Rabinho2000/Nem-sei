"""Assemble one `NotificationContext` -- the only place in this pipeline
that touches the database to build what a Telegram message needs.

Architecture (req 16): everything upstream (`episodes.py`, `eligibility.py`)
decided *whether*; everything downstream (`priority.py`, `impact.py`,
`playbook.py`, `render_telegram.py`) is a pure function of plain values. This
module is the seam between the two -- it reads `Installation`, `Asset`,
`AssetServiceContract`, `WorkOrder`, `InstallationContact` once per episode
and hands every other module in this package exactly the inputs it declared
it needs, never a session.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Device
from nemsei.contracts.priority import commercial_family, service_priority
from nemsei.contracts.service import om_status
from nemsei.diagnostics.incidents import recurrence_count
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.contacts import primary_or_first_contact
from nemsei.installations.models import Installation
from nemsei.notifications.impact import EnergyImpact, FinancialImpact, estimate_energy_impact, estimate_financial_impact
from nemsei.notifications.models import NotificationEpisode
from nemsei.notifications.playbook import suggested_action
from nemsei.notifications.priority import PriorityInputs, PriorityScore, score_episode
from nemsei.notifications.problem_families import category_for
from nemsei.reporting.commercial import representative_price, resolve_tariff
from nemsei.work_orders.models import WorkOrder
from nemsei.work_orders.service import work_orders_for_incident


@dataclass(frozen=True)
class NotificationContext:
    episode: NotificationEpisode
    incident: DiagnosticIncident
    asset: Asset
    device: Device | None
    installation: Installation | None
    category: str  # notifications.problem_families.CATEGORIES
    contract_family: str  # esco | esco_buyout | epc | unknown
    contract_family_label: str
    om_status: str  # active | expired | undated | none
    is_om: bool
    is_esco_priority: bool
    priority: PriorityScore
    recurrence_count_24h: int
    energy_impact: EnergyImpact
    financial_impact: FinancialImpact
    suggested_action: str
    work_order: WorkOrder | None  # the most recent one addressing this episode's current incident, if any
    work_planned_or_in_progress_today: bool
    contact_name: str | None
    contact_role: str | None
    contact_phone: str | None
    contact_email: str | None


def build_context(session: Session, *, episode: NotificationEpisode, now: datetime | None = None) -> NotificationContext:
    from nemsei.shared.clock import utc_now

    now_value = now or utc_now()
    incident = session.get(DiagnosticIncident, episode.last_incident_id)
    if incident is None:
        raise ValueError(f"NotificationEpisode {episode.id} points at a missing incident.")
    asset = session.get(Asset, episode.asset_id)
    if asset is None:
        raise ValueError(f"NotificationEpisode {episode.id} points at a missing asset.")
    device = session.get(Device, episode.device_id) if episode.device_id else None
    installation = session.get(Installation, asset.installation_id) if asset.installation_id else None

    on = now_value.date()
    om = om_status(session, asset_id=asset.id, on=on)
    family = commercial_family(asset.contract_type)
    priority_family = service_priority(family=family, om_status=om)

    work_orders = work_orders_for_incident(session, incident_id=incident.id)
    work_order = work_orders[-1] if work_orders else None
    has_work_order = bool(work_orders)
    work_planned_today = _work_planned_or_in_progress(work_orders, on=on)

    latitude = installation.latitude if installation else None
    longitude = installation.longitude if installation else None
    window_end = episode.closed_at or now_value
    energy = estimate_energy_impact(
        session, asset_id=asset.id, device_id=episode.device_id, start=episode.opened_at, end=window_end,
        latitude=latitude, longitude=longitude,
    )
    tariff = resolve_tariff(session, asset_id=asset.id, on=on)
    price = representative_price(tariff) if tariff is not None else None
    financial = estimate_financial_impact(energy=energy, price_eur_per_kwh=price)

    recurrence = recurrence_count(session, rule_code=incident.rule_code, asset_id=asset.id, device_id=episode.device_id, at=now_value)

    priority = score_episode(
        PriorityInputs(
            problem_family=episode.problem_family,
            severity_peak=episode.severity_peak,
            opened_at=episode.opened_at,
            now=now_value,
            commercial_family=family,
            om_status=om,
            installed_dc_power_kw=asset.installed_dc_power_kw,
            impact_eur=financial.lost_eur,
            recurrence_count_24h=recurrence,
            has_work_order=has_work_order,
            work_planned_or_in_progress_today=work_planned_today,
            recovered=episode.status == "closed",
        )
    )

    contact = primary_or_first_contact(session, installation_id=installation.id) if installation else None
    is_meter = "meter" in incident.rule_code.lower()
    action = suggested_action(
        problem_family=episode.problem_family, rule_code=incident.rule_code,
        is_esco=priority_family == "high", is_meter=is_meter,
    )

    return NotificationContext(
        episode=episode, incident=incident, asset=asset, device=device, installation=installation,
        category=category_for(episode.problem_family),
        contract_family=family, contract_family_label=_FAMILY_LABELS.get(family, family),
        om_status=om, is_om=om in ("active", "undated"), is_esco_priority=priority_family == "high",
        priority=priority, recurrence_count_24h=recurrence, energy_impact=energy, financial_impact=financial,
        suggested_action=action,
        work_order=work_order, work_planned_or_in_progress_today=work_planned_today,
        contact_name=contact.name if contact else None,
        contact_role=contact.role if contact else None, contact_phone=contact.phone if contact else None,
        contact_email=contact.email if contact else None,
    )


_FAMILY_LABELS = {"esco": "ESCO", "esco_buyout": "ESCO buyout", "epc": "EPC", "unknown": "Sem contrato registado"}


def _work_planned_or_in_progress(work_orders: list[WorkOrder], *, on: date) -> bool:
    return any(
        work_order.status == "in_progress" or (work_order.status == "planned" and work_order.planned_date == on)
        for work_order in work_orders
    )
