"""Telegram message rendering (req 9/17) -- pure, no database, no network."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from nemsei.assets.models import Asset
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.models import Installation
from nemsei.notifications.enrichment import NotificationContext
from nemsei.notifications.impact import EnergyImpact, FinancialImpact
from nemsei.notifications.models import NotificationEpisode
from nemsei.notifications.priority import PriorityScore
from nemsei.notifications.render_telegram import render_message
from nemsei.work_orders.models import WorkOrder


def utc(hour: int = 9, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def context(**overrides) -> NotificationContext:
    asset = Asset(
        id=1, public_id="a", canonical_name="DIACO", normalized_name="diaco", lifecycle_status="unknown",
        timezone_source="manual", installed_dc_power_kw=Decimal("430"), review_status="clear",
        created_at=utc(), updated_at=utc(),
    )
    installation = Installation(id=1, display_name="DIACO", timezone_source="manual", created_at=utc(), updated_at=utc())
    incident = DiagnosticIncident(
        id=1, rule_code="plant_offline", asset_id=1, device_id=None, severity="critical", status="open",
        opened_at=utc(5, 47), last_observed_at=utc(5, 47), occurrence_count=1, detector_version="2",
        evidence_json={}, created_at=utc(5, 47), updated_at=utc(5, 47),
    )
    episode = NotificationEpisode(
        id=1, asset_id=1, device_id=None, problem_family="communication", status="open", severity_peak="critical",
        opened_at=utc(5, 47), last_activity_at=utc(9), closed_at=None, flap_count=1, first_incident_id=1,
        last_incident_id=1, eligible_at=utc(6, 17), notified_at=utc(9), reminder_count=0, recovery_notified=False,
        created_at=utc(5, 47), updated_at=utc(9),
    )
    defaults = dict(
        episode=episode, incident=incident, asset=asset, device=None, installation=installation,
        category="communication_issue", contract_family="esco", contract_family_label="ESCO",
        om_status="active", is_om=True, is_esco_priority=True,
        priority=PriorityScore(score=60, bucket="MEDIUM", reasons=["Instalação sem comunicação"]),
        energy_impact=EnergyImpact(Decimal("620"), "..."), financial_impact=FinancialImpact(Decimal("84"), "..."),
        suggested_action="Verificar provider.\nConfirmar se toda a instalação está sem dados.",
        work_order=None, contact_name="João Silva", contact_role="Facilities",
        contact_phone="+351 9xx xxx xxx", contact_email=None,
    )
    defaults.update(overrides)
    return NotificationContext(**defaults)


# --- immediate alert (opened) ----------------------------------------------------


def test_opened_message_carries_the_installation_name_and_duration() -> None:
    text = render_message(context(), kind="opened", now=utc(9, 0))
    assert "DIACO" in text
    assert "Sem comunicação" in text
    assert "Offline há 3h13" in text
    assert "430 kWp" in text
    assert "O&M + ESCO" in text


def test_opened_message_shows_impact_when_calculable() -> None:
    text = render_message(context(), kind="opened", now=utc(9, 0))
    assert "620" in text and "84" in text


def test_opened_message_says_not_calculable_when_no_impact_exists() -> None:
    ctx = context(energy_impact=EnergyImpact(None, "sem leitura"), financial_impact=FinancialImpact(None, "sem tarifa"))
    text = render_message(ctx, kind="opened", now=utc(9, 0))
    assert "não calculável" in text


def test_opened_message_shows_the_contact_when_registered() -> None:
    text = render_message(context(), kind="opened", now=utc(9, 0))
    assert "João Silva · Facilities" in text
    assert "+351 9xx xxx xxx" in text


def test_opened_message_says_not_registered_when_there_is_no_contact() -> None:
    text = render_message(context(contact_name=None, contact_role=None, contact_phone=None), kind="opened", now=utc(9, 0))
    assert "Contacto local: não registado" in text


def test_opened_message_shows_no_work_when_none_exists() -> None:
    text = render_message(context(), kind="opened", now=utc(9, 0))
    assert "Trabalho aberto: não" in text


def test_opened_message_shows_the_work_order_reference_when_one_exists() -> None:
    wo = WorkOrder(
        id=142, public_id="wo1", installation_id=1, work_type="corrective", status="planned", title="String fault",
        material_status="not_applicable", planned_date=date(2026, 7, 24, ), created_by="ops",
        created_at=utc(), updated_at=utc(),
    )
    text = render_message(context(work_order=wo), kind="opened", now=utc(9, 0))
    assert "WO-142" in text
    assert "não" not in text.split("Trabalho aberto:")[1].splitlines()[0]


def test_no_base_url_omits_the_links_line() -> None:
    text = render_message(context(), kind="opened", now=utc(9, 0), base_url=None)
    assert "diagnostics" not in text


def test_a_base_url_adds_installation_and_incident_links() -> None:
    text = render_message(context(), kind="opened", now=utc(9, 0), base_url="https://ops.solcor.pt")
    assert "https://ops.solcor.pt/diagnostics/assets/1" in text
    assert "https://ops.solcor.pt/diagnostics/incidents/1" in text


def test_a_fault_family_reads_aberto_not_offline() -> None:
    episode = NotificationEpisode(
        id=2, asset_id=1, device_id=None, problem_family="fault", status="open", severity_peak="critical",
        opened_at=utc(5, 47), last_activity_at=utc(9), closed_at=None, flap_count=1, first_incident_id=1,
        last_incident_id=1, eligible_at=utc(5, 47), notified_at=utc(9), reminder_count=0, recovery_notified=False,
        created_at=utc(5, 47), updated_at=utc(9),
    )
    text = render_message(context(episode=episode), kind="opened", now=utc(9, 0))
    assert "Aberto há" in text
    assert "Offline há" not in text


# --- reminder / escalated ---------------------------------------------------------


def test_reminder_message_says_still_open() -> None:
    text = render_message(context(), kind="reminder", now=utc(9, 47))
    assert "ainda aberto" in text.lower()


def test_escalated_message_says_escalated() -> None:
    text = render_message(context(), kind="escalated", now=utc(9, 47))
    assert "escalado" in text.lower()


# --- recovery -----------------------------------------------------------------


def test_recovery_message_shows_duration_and_a_green_icon() -> None:
    closed_episode = NotificationEpisode(
        id=1, asset_id=1, device_id=None, problem_family="communication", status="closed", severity_peak="critical",
        opened_at=utc(5, 47), last_activity_at=utc(9), closed_at=utc(9, 0), flap_count=1, first_incident_id=1,
        last_incident_id=1, eligible_at=utc(6, 17), notified_at=utc(6, 17), reminder_count=0, recovery_notified=False,
        created_at=utc(5, 47), updated_at=utc(9),
    )
    ctx = context(episode=closed_episode)
    text = render_message(ctx, kind="resolved", now=utc(9, 30))
    assert text.startswith("🟢")
    assert "recuperado" in text
    assert "Durou 3h13" in text


def test_recovery_message_mentions_flap_count_when_it_flapped() -> None:
    flapped_episode = NotificationEpisode(
        id=1, asset_id=1, device_id=None, problem_family="communication", status="closed", severity_peak="critical",
        opened_at=utc(5, 47), last_activity_at=utc(9), closed_at=utc(9, 0), flap_count=3, first_incident_id=1,
        last_incident_id=3, eligible_at=utc(6, 17), notified_at=utc(6, 17), reminder_count=0, recovery_notified=False,
        created_at=utc(5, 47), updated_at=utc(9),
    )
    text = render_message(context(episode=flapped_episode), kind="resolved", now=utc(9, 30))
    assert "3 ocorrências" in text
