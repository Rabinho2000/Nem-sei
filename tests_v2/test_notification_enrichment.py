"""`build_context` (Telegram O&M redesign): the one seam that touches the
database to assemble a `NotificationContext` -- an integration smoke test
that every other module in this pipeline (priority, impact, playbook,
contacts) is wired together correctly, end to end.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from nemsei.assets.service import create_asset
from nemsei.contracts.service import set_service_contract
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.contacts import add_contact
from nemsei.installations.models import Installation
from nemsei.notifications.enrichment import build_context
from nemsei.notifications.episodes import sync_episodes
from nemsei.notifications.models import NotificationEpisode
from nemsei.work_orders.service import create_work_order


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def test_build_context_assembles_an_esco_installation_with_a_contact_and_no_work_order(factory) -> None:
    with factory() as session, session.begin():
        installation = Installation(
            display_name="DIACO", timezone_source="manual", latitude=Decimal("38.7223"), longitude=Decimal("-9.1393"),
            coordinates_source="manual", coordinates_confidence="ok", created_at=utc(9), updated_at=utc(9),
        )
        session.add(installation)
        session.flush()
        asset = create_asset(session, canonical_name="DIACO", installed_dc_power_kw=Decimal("430"))
        asset.installation_id = installation.id
        asset.contract_type = "ESCO"
        set_service_contract(session, asset_id=asset.id, created_by="ops", valid_from=date(2020, 1, 1), valid_to=None, service_kind="om")
        add_contact(
            session, installation_id=installation.id, name="João Silva", role="Facilities",
            phone="+351 9xx xxx xxx", contact_type="facility_manager", is_primary=True, created_by="ops",
        )
        incident = DiagnosticIncident(
            rule_code="plant_offline", asset_id=asset.id, device_id=None, severity="critical", status="open",
            opened_at=utc(5), last_observed_at=utc(9), occurrence_count=1, detector_version="2", evidence_json={},
            created_at=utc(5), updated_at=utc(9),
        )
        session.add(incident)
        session.flush()
        sync_episodes(session, now=utc(9))
        asset_id = asset.id

    with factory() as session:
        episode = session.scalar(select(NotificationEpisode).where(NotificationEpisode.asset_id == asset_id))
        context = build_context(session, episode=episode, now=utc(9))

    assert context.asset.canonical_name == "DIACO"
    assert context.installation is not None and context.installation.display_name == "DIACO"
    assert context.contract_family == "esco"
    assert context.is_om is True
    assert context.is_esco_priority is True
    assert context.contact_name == "João Silva"
    assert context.contact_phone == "+351 9xx xxx xxx"
    assert context.work_order is None
    assert context.category == "communication_issue"
    assert "provider" in context.suggested_action.lower()
    assert context.priority.score > 0
    assert context.priority.reasons  # never hidden


def test_build_context_finds_the_linked_work_order(factory) -> None:
    with factory() as session, session.begin():
        installation = Installation(display_name="Plant", timezone_source="manual", created_at=utc(9), updated_at=utc(9))
        session.add(installation)
        session.flush()
        asset = create_asset(session, canonical_name="Plant")
        asset.installation_id = installation.id
        incident = DiagnosticIncident(
            rule_code="device_unavailable", asset_id=asset.id, device_id=None, severity="critical", status="open",
            opened_at=utc(9), last_observed_at=utc(9), occurrence_count=1, detector_version="2", evidence_json={},
            created_at=utc(9), updated_at=utc(9),
        )
        session.add(incident)
        session.flush()
        create_work_order(
            session, installation_id=installation.id, work_type="corrective", title="Inverter fault",
            created_by="ops", status="planned", planned_date=date(2026, 7, 24), incident_ids=[incident.id],
        )
        sync_episodes(session, now=utc(9))
        asset_id = asset.id

    with factory() as session:
        episode = session.scalar(select(NotificationEpisode).where(NotificationEpisode.asset_id == asset_id))
        context = build_context(session, episode=episode, now=utc(9))

    assert context.work_order is not None
    assert context.work_order.title == "Inverter fault"
    assert context.priority.score < 100  # no-work-order and duration points both suppressed/absent as applicable
    assert not any("Sem WorkOrder" in reason for reason in context.priority.reasons)


def test_build_context_says_not_registered_when_there_is_no_contact(factory) -> None:
    with factory() as session, session.begin():
        installation = Installation(display_name="Plant", timezone_source="manual", created_at=utc(9), updated_at=utc(9))
        session.add(installation)
        session.flush()
        asset = create_asset(session, canonical_name="Plant")
        asset.installation_id = installation.id
        incident = DiagnosticIncident(
            rule_code="stale_reading", asset_id=asset.id, device_id=None, severity="warning", status="open",
            opened_at=utc(9), last_observed_at=utc(9), occurrence_count=1, detector_version="2", evidence_json={},
            created_at=utc(9), updated_at=utc(9),
        )
        session.add(incident)
        session.flush()
        sync_episodes(session, now=utc(9))
        asset_id = asset.id

    with factory() as session:
        episode = session.scalar(select(NotificationEpisode).where(NotificationEpisode.asset_id == asset_id))
        context = build_context(session, episode=episode, now=utc(9))

    assert context.contact_name is None
    assert context.category == "monitoring_coverage"  # never presented as an operational fault
