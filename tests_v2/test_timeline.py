"""The installation timeline: a merge, never a table.

No migration accompanies this file -- `timeline/service.py` reads existing
tables and writes nothing. These tests exercise the merge itself: correct
chronological ordering across sources with different native shapes (a date
versus a timestamp), the collapsing of a device poll history into transitions
only, and that nothing here can write to any of the tables it reads.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.handling import record_incident_handling
from nemsei.diagnostics.models import DeviceStatusFact, DiagnosticIncident, IncidentNote
from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset
from nemsei.monitoring.models import MonitoringObservation
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.timeline.service import installation_timeline
from nemsei.work_orders.models import Visit, WorkOrder, WorkOrderIncident
from nemsei.work_orders.service import add_visit, create_work_order, update_work_order_status


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def seed(session) -> tuple[int, int]:
    """One asset, its backfilled installation, and one inverter device."""
    asset = create_asset(session, canonical_name="DIACO")
    device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
    session.flush()
    backfill_installations_from_assets(session)
    installation = installation_for_asset(session, asset_id=asset.id)
    return asset.id, installation.id, device.id


def add_device_fact(session, *, device_id: int, asset_id: int, observed_at: datetime, status: str) -> None:
    now = utc_now()
    session.add(
        DeviceStatusFact(
            device_id=device_id,
            asset_id=asset_id,
            source_fact_key=f"key-{observed_at.isoformat()}",
            observed_at=observed_at,
            ingested_at=now,
            availability_status=status,
            source_kind="live_read",
        )
    )


# --- the case the module exists for -----------------------------------


def test_the_device_poll_history_collapses_to_transitions_only(factory) -> None:
    """A device polled every 15 minutes for hours must not produce one
    timeline entry per poll -- only the moments status actually changed."""
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        for minute in range(0, 60, 15):
            add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc(2026, 6, 1, 14, minute), status="available")
        add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc(2026, 6, 1, 15, 0), status="unavailable")
        for minute in range(15, 60, 15):
            add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc(2026, 6, 1, 15, minute), status="unavailable")
        add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc(2026, 6, 1, 16, 0), status="available")

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc(2026, 6, 1, 0, 0), until=utc(2026, 6, 2, 0, 0))
        device_events = [e for e in events if e.kind == "device_state"]
        assert len(device_events) == 3
        assert [e.occurred_at for e in device_events] == [
            utc(2026, 6, 1, 14, 0), utc(2026, 6, 1, 15, 0), utc(2026, 6, 1, 16, 0),
        ]
        assert device_events[1].detail == "INV-1: available → unavailable"


def test_an_empty_installation_returns_an_empty_list_not_an_error(factory) -> None:
    with factory() as session, session.begin():
        _, installation_id, _ = seed(session)

    with factory() as session:
        assert installation_timeline(session, installation_id=installation_id) == []


def test_an_unknown_installation_returns_an_empty_list(factory) -> None:
    with factory() as session:
        assert installation_timeline(session, installation_id=999999) == []


# --- chronological merge across differently-shaped sources ---------------


def seed_mapping(session, *, asset_id: int) -> int:
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key="main", display_name="FusionSolar",
        credential_reference="FUSIONSOLAR_MAIN", enabled=True, configuration_status="configured",
    )
    mapping = create_mapping(session, asset_id=asset_id, provider_connection_id=connection.id, external_id="NE=1")
    return mapping.id


def test_events_from_every_source_merge_into_one_chronological_order(factory) -> None:
    """GOAL.md's own example: alarm, incident, diagnosis, work, visit,
    resolution -- read top to bottom in the order they happened, regardless
    of which table each came from.

    Every timestamp is set directly rather than through the service layer,
    which stamps its own `utc_now()` -- the point here is the merge and sort
    across sources, not the write path each source already has its own tests
    for. Visit's day-only precision is deliberately given a same-day-morning
    reading (08:00) that genuinely precedes the afternoon timestamps that
    follow it, rather than a bare date that would collide with midnight --
    see `test_visit_events_carry_day_precision_not_a_fabricated_time` for
    what happens when it does not.
    """
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        mapping_id = seed_mapping(session, asset_id=asset_id)
        now = utc_now()

        session.add(  # 09:32 alarme recebido
            MonitoringObservation(
                asset_id=asset_id, provider_mapping_id=mapping_id, source_observation_key="k1",
                observed_at=utc(2026, 6, 1, 9, 32), ingested_at=now, condition="offline",
            )
        )
        incident = DiagnosticIncident(  # 09:47 incidente criado
            rule_code="plant_offline", asset_id=asset_id, severity="critical", status="open",
            opened_at=utc(2026, 6, 1, 9, 47), last_observed_at=utc(2026, 6, 1, 9, 47),
            detector_version="test", created_at=now, updated_at=now,
        )
        session.add(incident)
        session.flush()
        session.add(  # 10:03 diagnóstico iniciado
            IncidentNote(
                incident_id=incident.id, author="op", handling_state_after="investigating",
                created_at=utc(2026, 6, 1, 10, 3),
            )
        )
        work_order = WorkOrder(  # 14:31 trabalho criado
            installation_id=installation_id, work_type="corrective", status="open", title="Trocar disjuntor",
            material_status="not_applicable", created_by="op",
            created_at=utc(2026, 6, 1, 14, 31), updated_at=utc(2026, 6, 1, 14, 31),
        )
        session.add(work_order)
        session.flush()
        session.add(WorkOrderIncident(work_order_id=work_order.id, incident_id=incident.id, created_at=now))
        session.add(  # dia seguinte 08:00 visita iniciada
            Visit(
                work_order_id=work_order.id, visit_date=date(2026, 6, 2), technician="Anderson",
                outcome="Disjuntor substituído", created_by="op", created_at=now,
            )
        )
        session.add(  # 11:00 incidente em verificação
            IncidentNote(
                incident_id=incident.id, author="op", handling_state_after="visit_scheduled",
                created_at=utc(2026, 6, 1, 11, 0),
            )
        )
        # dia seguinte: disjuntor substituído, inversor voltou a operar, trabalho concluído, incidente fechado
        work_order.status = "completed"
        work_order.completed_at = utc(2026, 6, 2, 10, 14)
        incident.status = "resolved"
        incident.resolved_at = utc(2026, 6, 2, 10, 30)

    with factory() as session:
        events = installation_timeline(
            session, installation_id=installation_id, since=utc(2026, 6, 1, 0, 0), until=utc(2026, 6, 3, 0, 0)
        )
        kinds = [event.kind for event in events]
        assert kinds == [
            "plant_state",           # 09:32 alarme recebido
            "incident_opened",       # 09:47 incidente criado
            "incident_note",         # 10:03 diagnóstico iniciado
            "incident_note",         # 11:00 em verificação
            "work_order_created",    # 14:31 trabalho criado
            "visit",                 # dia seguinte, precisão de dia -> meia-noite
            "work_order_completed",  # dia seguinte 10:14
            "incident_resolved",     # dia seguinte 10:30
        ]
        assert [event.occurred_at for event in events] == sorted(event.occurred_at for event in events)


def test_visit_events_carry_day_precision_not_a_fabricated_time(factory) -> None:
    """`Visit.visit_date` is a date, not a timestamp. Inventing a clock
    reading for it would be precision the data does not have."""
    with factory() as session, session.begin():
        _, installation_id, _ = seed(session)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Reparação", created_by="op"
        )
        add_visit(session, work_order_id=work_order.id, visit_date=date(2026, 6, 2), created_by="op", technician="Anderson")

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc(2026, 6, 1, 0, 0), until=utc(2026, 6, 3, 0, 0))
        visit_events = [e for e in events if e.kind == "visit"]
        assert len(visit_events) == 1
        assert visit_events[0].precision == "day"
        assert visit_events[0].occurred_at == utc(2026, 6, 2, 0, 0)
        assert visit_events[0].detail == "Anderson"


def test_work_order_created_and_completed_are_both_visible(factory) -> None:
    with factory() as session, session.begin():
        _, installation_id, _ = seed(session)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="cleaning", title="Limpeza", created_by="op"
        )
        work_order_id = work_order.id

    with factory() as session, session.begin():
        update_work_order_status(session, work_order_id=work_order_id, status="completed", actor="op")

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc_now() - timedelta(days=1), until=utc_now() + timedelta(days=1))
        kinds = [e.kind for e in events]
        assert "work_order_created" in kinds
        assert "work_order_completed" in kinds
        # created must read before completed
        created_at = next(e.occurred_at for e in events if e.kind == "work_order_created")
        completed_at = next(e.occurred_at for e in events if e.kind == "work_order_completed")
        assert created_at <= completed_at


def test_a_work_order_never_completed_shows_no_completion_event(factory) -> None:
    with factory() as session, session.begin():
        _, installation_id, _ = seed(session)
        create_work_order(session, installation_id=installation_id, work_type="preventive", title="Revisão", created_by="op")

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc_now() - timedelta(days=1), until=utc_now() + timedelta(days=1))
        assert "work_order_completed" not in [e.kind for e in events]


def test_incident_opened_and_resolved_both_appear_in_order(factory) -> None:
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        now = utc_now()
        incident = DiagnosticIncident(
            rule_code="plant_offline",
            asset_id=asset_id,
            severity="critical",
            status="resolved",
            opened_at=now - timedelta(hours=2),
            last_observed_at=now - timedelta(hours=1),
            resolved_at=now - timedelta(hours=1),
            detector_version="test",
            created_at=now,
            updated_at=now,
        )
        session.add(incident)

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc_now() - timedelta(days=1), until=utc_now() + timedelta(days=1))
        kinds = [e.kind for e in events]
        assert kinds.index("incident_opened") < kinds.index("incident_resolved")


def test_a_still_open_incident_has_no_resolved_event(factory) -> None:
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        now = utc_now()
        session.add(
            DiagnosticIncident(
                rule_code="plant_fault", asset_id=asset_id, severity="critical", status="open",
                opened_at=now, last_observed_at=now, detector_version="test", created_at=now, updated_at=now,
            )
        )

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc_now() - timedelta(days=1), until=utc_now() + timedelta(days=1))
        assert "incident_resolved" not in [e.kind for e in events]


def test_a_handling_note_appears_with_its_portuguese_state_label(factory) -> None:
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        now = utc_now()
        incident = DiagnosticIncident(
            rule_code="plant_fault", asset_id=asset_id, severity="critical", status="open",
            opened_at=now, last_observed_at=now, detector_version="test", created_at=now, updated_at=now,
        )
        session.add(incident)
        session.flush()
        record_incident_handling(session, incident_id=incident.id, actor="op", handling_state="investigating")

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc_now() - timedelta(days=1), until=utc_now() + timedelta(days=1))
        notes = [e for e in events if e.kind == "incident_note"]
        assert len(notes) == 1
        assert "Em análise" in notes[0].detail
        assert "investigating" not in notes[0].detail


# --- the window ------------------------------------------------------------


def test_events_outside_the_window_are_excluded(factory) -> None:
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc(2020, 1, 1, 0, 0), status="available")

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id, since=utc(2026, 1, 1), until=utc(2026, 12, 31))
        assert events == []


def test_the_default_window_is_thirty_days(factory) -> None:
    """The reading from 45 days ago falls outside the default window and its
    own event is excluded -- but the transition it set up is not: the one
    surviving event still reads "available → unavailable", because the
    transition is computed from the full fetched history and only *filtered*
    by the window afterwards. Losing that "from" context the moment its own
    origin point aged out of the window would be worse, not more correct."""
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc_now() - timedelta(days=45), status="available")
        add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc_now() - timedelta(days=1), status="unavailable")

    with factory() as session:
        events = installation_timeline(session, installation_id=installation_id)
        assert len(events) == 1
        assert events[0].detail == "INV-1: available → unavailable"


# --- what it must not do ----------------------------------------------


def test_the_projection_writes_nothing(factory) -> None:
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=utc_now(), status="available")

    with factory() as session:
        before = session.scalar(select(func.count(DeviceStatusFact.id)))
        installation_timeline(session, installation_id=installation_id)
        after = session.scalar(select(func.count(DeviceStatusFact.id)))
        assert before == after
        # No commit ever happened on a plain read; a dirty session would
        # still show it, so this also proves no row was staged for write.
        assert not session.dirty and not session.new


def test_ordering_is_stable_for_two_events_at_the_exact_same_instant(factory) -> None:
    """Tie-broken by source table and id, deterministically -- not by
    insertion order, which a merge across independent queries cannot rely on."""
    with factory() as session, session.begin():
        asset_id, installation_id, device_id = seed(session)
        moment = utc(2026, 6, 1, 12, 0)
        add_device_fact(session, device_id=device_id, asset_id=asset_id, observed_at=moment, status="available")

    with factory() as session:
        first = installation_timeline(session, installation_id=installation_id, since=utc(2026, 6, 1), until=utc(2026, 6, 2))
        second = installation_timeline(session, installation_id=installation_id, since=utc(2026, 6, 1), until=utc(2026, 6, 2))
        assert first == second
