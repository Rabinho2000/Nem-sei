"""Morning O&M briefing (Telegram O&M redesign, reqs 10-12/17).

The central constraint the request restated explicitly: the briefing must
reuse the *exact same* priority engine immediate alerts use
(`notifications.priority.score_episode`, via `enrichment.build_context`) --
never a second ranking. Every ordering test here proves that by comparing
the briefing's own numbers against a direct call to the same function, not
by re-deriving an expectation independently.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.service import create_asset
from nemsei.contracts.service import set_service_contract
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.contacts import add_contact
from nemsei.installations.models import Installation
from nemsei.notifications.digests import build_morning_briefing_payload, render_morning_briefing_text
from nemsei.notifications.enrichment import build_context
from nemsei.notifications.models import NotificationEpisode
from nemsei.work_orders.service import create_work_order


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int = 9, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def make_om_installation(session, *, name: str, installed_dc_power_kw: Decimal | None = None, contract_type: str | None = None) -> tuple[int, int]:
    installation = Installation(display_name=name, timezone_source="manual", created_at=utc(), updated_at=utc())
    session.add(installation)
    session.flush()
    asset = create_asset(session, canonical_name=name, installed_dc_power_kw=installed_dc_power_kw)
    asset.installation_id = installation.id
    if contract_type:
        asset.contract_type = contract_type
    set_service_contract(session, asset_id=asset.id, created_by="ops", valid_from=date(2020, 1, 1), valid_to=None, service_kind="om")
    session.flush()
    return asset.id, installation.id


def make_open_episode(
    session, *, asset_id: int, opened_at: datetime, severity_peak: str = "critical",
    problem_family: str = "fault", notified_at: datetime | None = None, rule_code: str = "device_unavailable",
) -> NotificationEpisode:
    incident = DiagnosticIncident(
        rule_code=rule_code, asset_id=asset_id, device_id=None, severity=severity_peak, status="open",
        opened_at=opened_at, last_observed_at=opened_at, occurrence_count=1, detector_version="2",
        evidence_json={}, created_at=opened_at, updated_at=opened_at,
    )
    session.add(incident)
    session.flush()
    episode = NotificationEpisode(
        asset_id=asset_id, device_id=None, problem_family=problem_family, status="open", severity_peak=severity_peak,
        opened_at=opened_at, last_activity_at=opened_at, flap_count=1, first_incident_id=incident.id,
        last_incident_id=incident.id, eligible_at=opened_at, notified_at=notified_at or opened_at,
        reminder_count=0, recovery_notified=False, created_at=opened_at, updated_at=opened_at,
    )
    session.add(episode)
    session.flush()
    return episode


# --- reuses the exact same engine, never a second ranking -----------------------


def test_the_briefing_score_is_identical_to_a_direct_priority_call(factory) -> None:
    with factory() as session, session.begin():
        asset_id, _ = make_om_installation(session, name="DIACO", installed_dc_power_kw=Decimal("430"), contract_type="ESCO")
        episode = make_open_episode(session, asset_id=asset_id, opened_at=utc(9), problem_family="communication", rule_code="plant_offline")
        direct_context = build_context(session, episode=episode, now=utc(14))

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(14))

    assert len(payload["high"]) == 1
    row = payload["high"][0]
    assert row["priority_score"] == direct_context.priority.score
    assert row["priority_bucket"] == direct_context.priority.bucket
    assert row["priority_reasons"] == direct_context.priority.reasons


# --- ordering: a small, already-planned fault never outranks a big untouched one


def test_a_small_planned_fault_never_outranks_a_large_untouched_outage(factory) -> None:
    with factory() as session, session.begin():
        big_id, big_install_id = make_om_installation(session, name="Big Plant", installed_dc_power_kw=Decimal("800"), contract_type="ESCO")
        make_open_episode(session, asset_id=big_id, opened_at=utc(9), problem_family="communication", rule_code="plant_offline")

        small_id, small_install_id = make_om_installation(session, name="Small Plant", installed_dc_power_kw=Decimal("10"))
        small_episode = make_open_episode(session, asset_id=small_id, opened_at=utc(13), problem_family="fault", rule_code="device_unavailable")
        create_work_order(
            session, installation_id=small_install_id, work_type="corrective", title="Small fix",
            created_by="ops", status="planned", planned_date=utc(14).date(),
            incident_ids=[small_episode.last_incident_id],
        )

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(14))

    names_in_order = [row["name"] for row in payload["high"] + payload["to_check"]]
    assert names_in_order.index("Big Plant") < names_in_order.index("Small Plant")


# --- only O&M-active installations (req 1) --------------------------------------


def test_an_installation_without_om_never_appears(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="No O&M")
        incident = DiagnosticIncident(
            rule_code="plant_offline", asset_id=asset.id, device_id=None, severity="critical", status="open",
            opened_at=utc(9), last_observed_at=utc(9), occurrence_count=1, detector_version="2", evidence_json={},
            created_at=utc(9), updated_at=utc(9),
        )
        session.add(incident)
        session.flush()
        session.add(NotificationEpisode(
            asset_id=asset.id, device_id=None, problem_family="communication", status="open", severity_peak="critical",
            opened_at=utc(9), last_activity_at=utc(9), flap_count=1, first_incident_id=incident.id,
            last_incident_id=incident.id, eligible_at=utc(9), notified_at=utc(9), reminder_count=0,
            recovery_notified=False, created_at=utc(9), updated_at=utc(9),
        ))

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(14))
    assert payload["om_active_count"] == 0
    assert payload["high"] == [] and payload["to_check"] == []


# --- category distinction: coverage is never presented as a fault (req 12) -----


def test_a_coverage_only_installation_counts_as_insufficient_data_not_a_fault(factory) -> None:
    with factory() as session, session.begin():
        asset_id, _ = make_om_installation(session, name="Sparse Data")
        make_open_episode(session, asset_id=asset_id, opened_at=utc(9), severity_peak="warning", problem_family="coverage", rule_code="stale_reading")

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(14))

    assert payload["category_counts"]["monitoring_coverage"] == 1
    assert payload["category_counts"]["operational_fault"] == 0
    row = (payload["high"] + payload["to_check"])[0]
    assert row["category"] == "monitoring_coverage"
    text = render_morning_briefing_text(payload)
    assert "sem dados suficientes" in text
    assert "1 fault" not in text  # never miscounted as a fault


def test_operational_count_excludes_installations_with_an_open_episode(factory) -> None:
    with factory() as session, session.begin():
        make_om_installation(session, name="Healthy 1")
        make_om_installation(session, name="Healthy 2")
        broken_id, _ = make_om_installation(session, name="Broken")
        make_open_episode(session, asset_id=broken_id, opened_at=utc(9))

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(14))
    assert payload["om_active_count"] == 3
    assert payload["operational_count"] == 2


# --- WorkOrder / visit shown when it exists (reqs 10, 14) -----------------------


def test_a_planned_visit_reduces_no_action_and_is_shown(factory) -> None:
    with factory() as session, session.begin():
        # 800 kWp, not 432: the -30 already-planned-work penalty (req 5) can
        # legitimately drop a communication+ESCO episode's score below the
        # HIGH threshold on its own -- that is priority.py working as
        # designed, not a test artefact to route around. The extra >500kWp
        # component keeps this one in HIGH so `no_action_count` (a HIGH-only
        # count) is the thing actually being proven, not an empty list.
        asset_id, installation_id = make_om_installation(session, name="ITECMO", installed_dc_power_kw=Decimal("800"), contract_type="ESCO")
        episode = make_open_episode(session, asset_id=asset_id, opened_at=utc(9, 0, day=23), problem_family="communication", rule_code="plant_offline")
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Fix comms",
            created_by="ops", status="in_progress", incident_ids=[episode.last_incident_id],
        )

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(11))

    assert payload["no_action_count"] == 0
    row = (payload["high"] + payload["to_check"])[0]
    assert row["work_order_label"] is not None and row["work_order_label"].startswith("WO-")
    text = render_morning_briefing_text(payload)
    assert "Trabalho:" in text
    assert "Sem trabalho aberto" not in text


def test_no_work_order_shows_sem_trabalho_and_counts_as_no_action(factory) -> None:
    with factory() as session, session.begin():
        asset_id, _ = make_om_installation(session, name="ITECMO", installed_dc_power_kw=Decimal("432"), contract_type="ESCO")
        make_open_episode(session, asset_id=asset_id, opened_at=utc(9, 0, day=23), problem_family="communication", rule_code="plant_offline")

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(11))

    assert payload["no_action_count"] == 1
    text = render_morning_briefing_text(payload)
    assert "Sem trabalho aberto" in text


# --- contact + suggested action shown when useful -------------------------------


def test_contact_and_suggested_action_appear_when_registered(factory) -> None:
    with factory() as session, session.begin():
        asset_id, installation_id = make_om_installation(session, name="DIACO", contract_type="ESCO")
        add_contact(session, installation_id=installation_id, name="João Silva", role="Facilities", phone="+351 9", created_by="ops", is_primary=True)
        make_open_episode(session, asset_id=asset_id, opened_at=utc(9), problem_family="communication", rule_code="plant_offline")

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(11))

    text = render_morning_briefing_text(payload)
    assert "João Silva" in text
    assert "provider" in text.lower()  # the plant_offline playbook action


# --- economic data: only when calculable (req: "não calculável") ---------------


def test_impact_reads_not_calculable_without_real_data(factory) -> None:
    with factory() as session, session.begin():
        asset_id, _ = make_om_installation(session, name="DIACO", contract_type="ESCO")
        make_open_episode(session, asset_id=asset_id, opened_at=utc(9), problem_family="communication", rule_code="plant_offline")

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(11))

    text = render_morning_briefing_text(payload)
    assert "não calculável" in text
    assert "€0" not in text and "0 kWh" not in text


# --- RECORRENTES: >=3 episodes/24h, req 5's own threshold ----------------------


def test_a_recurrent_problem_appears_in_the_recorrentes_section(factory) -> None:
    with factory() as session, session.begin():
        asset_id, _ = make_om_installation(session, name="Neutripuro")
        for hour in (2, 5, 8):
            past = DiagnosticIncident(
                rule_code="plant_offline", asset_id=asset_id, device_id=None, severity="critical", status="resolved",
                opened_at=utc(hour), last_observed_at=utc(hour, 10), resolved_at=utc(hour, 10), occurrence_count=1,
                detector_version="2", evidence_json={}, created_at=utc(hour), updated_at=utc(hour, 10),
            )
            session.add(past)
        session.flush()
        make_open_episode(session, asset_id=asset_id, opened_at=utc(9, 30), problem_family="communication", rule_code="plant_offline")

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(14))

    assert len(payload["recurring"]) == 1
    assert payload["recurring"][0]["name"] == "Neutripuro"
    assert payload["recurring"][0]["recurrence_count_24h"] >= 3
    text = render_morning_briefing_text(payload)
    assert "RECORRENTES" in text and "falhas/24h" in text


# --- render format smoke test ----------------------------------------------------


def test_render_includes_the_header_and_resumo_section(factory) -> None:
    with factory() as session, session.begin():
        make_om_installation(session, name="Healthy")

    with factory() as session:
        payload = build_morning_briefing_payload(session, now=utc(9))
    text = render_morning_briefing_text(payload)
    assert text.startswith("O&M — Estado do parque")
    assert "24/07/2026 · 09:00" in text
    assert "RESUMO" in text
    assert "1 contratos O&M ativos" in text
    assert "1 operacionais" in text
