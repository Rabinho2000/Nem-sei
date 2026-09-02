"""WorkOrder, Visit, and their many-to-many with DiagnosticIncident.

`DiagnosticIncident` is not touched by this feature at all -- these tests
prove that as directly as the schema tests: the evaluator's own columns, its
dedup index, and its 447-incident production shape are somebody else's
concern. What is new here is purely additive.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset
from nemsei.shared.clock import utc_now
from nemsei.work_orders.models import Visit, WorkOrder
from nemsei.work_orders.service import (
    add_visit,
    create_work_order,
    incidents_for_work_order,
    link_incident,
    open_work_order_counts,
    overdue_work_orders,
    unscheduled_work_orders,
    update_work_order_status,
    work_orders_for_incident,
    work_orders_for_installation,
)


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def seed_installation(session) -> int:
    asset = create_asset(session, canonical_name="Alpha")
    session.flush()
    backfill_installations_from_assets(session)
    return installation_for_asset(session, asset_id=asset.id).id


def seed_incident(session, *, asset_id: int, rule_code: str = "plant_offline") -> int:
    now = utc_now()
    incident = DiagnosticIncident(
        rule_code=rule_code,
        asset_id=asset_id,
        severity="critical",
        status="open",
        opened_at=now,
        last_observed_at=now,
        detector_version="test",
        created_at=now,
        updated_at=now,
    )
    session.add(incident)
    session.flush()
    return incident.id


# --- schema -----------------------------------------------------------------


def test_the_migration_creates_the_three_tables(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    tables = set(inspect(create_engine(settings.database_url)).get_table_names())
    assert {"work_orders", "visits", "work_order_incidents"} <= tables


def test_the_migration_downgrades_cleanly(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    command.downgrade(Config("alembic.ini"), "0033_work_orders-1")
    tables = set(inspect(create_engine(settings.database_url)).get_table_names())
    assert not ({"work_orders", "visits", "work_order_incidents"} & tables)
    assert "diagnostic_incidents" in tables
    command.upgrade(Config("alembic.ini"), "head")


def test_diagnostic_incidents_columns_are_unchanged_by_this_migration(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    columns = {c["name"] for c in inspect(create_engine(settings.database_url)).get_columns("diagnostic_incidents")}
    assert columns == {
        "id", "rule_code", "asset_id", "device_id", "severity", "status", "handling_state",
        "assigned_to", "handling_updated_at", "opened_at", "last_observed_at", "resolved_at",
        "occurrence_count", "detector_version", "evidence_json", "created_at", "updated_at",
    }


def test_an_unknown_work_type_is_refused_by_the_database(factory) -> None:
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            installation_id = seed_installation(session)
            session.execute(
                text(
                    "INSERT INTO work_orders (public_id, installation_id, work_type, status, title,"
                    " material_status, created_by, created_at, updated_at)"
                    " VALUES ('x', :installation_id, 'demolition', 'open', 't', 'not_applicable', 'op', now(), now())"
                ),
                {"installation_id": installation_id},
            )


def test_the_same_incident_cannot_be_linked_to_the_same_work_order_twice_at_the_database_level(factory) -> None:
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            installation_id = seed_installation(session)
            asset = create_asset(session, canonical_name="Beta")
            session.flush()
            incident_id = seed_incident(session, asset_id=asset.id)
            work_order = create_work_order(
                session, installation_id=installation_id, work_type="corrective", title="T", created_by="op"
            )
            session.execute(
                text(
                    "INSERT INTO work_order_incidents (work_order_id, incident_id, created_at) VALUES (:w, :i, now())"
                ),
                {"w": work_order.id, "i": incident_id},
            )
            session.execute(
                text(
                    "INSERT INTO work_order_incidents (work_order_id, incident_id, created_at) VALUES (:w, :i, now())"
                ),
                {"w": work_order.id, "i": incident_id},
            )


# --- creating and linking -----------------------------------------------


def test_creating_a_work_order_links_the_incidents_given_at_creation(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        asset = create_asset(session, canonical_name="Beta")
        session.flush()
        incident_id = seed_incident(session, asset_id=asset.id)

        work_order = create_work_order(
            session,
            installation_id=installation_id,
            work_type="corrective",
            title="Trocar disjuntor",
            created_by="op",
            incident_ids=[incident_id],
        )
        work_order_id = work_order.id

    with factory() as session:
        incidents = incidents_for_work_order(session, work_order_id=work_order_id)
        assert [incident.id for incident in incidents] == [incident_id]


def test_one_work_order_can_close_several_incidents(factory) -> None:
    """Three inverters offline on the same string fault is one job."""
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        asset = create_asset(session, canonical_name="Beta")
        session.flush()
        incident_a = seed_incident(session, asset_id=asset.id, rule_code="device_unavailable")
        incident_b = seed_incident(session, asset_id=asset.id, rule_code="stale_reading")
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op"
        )
        link_incident(session, work_order_id=work_order.id, incident_id=incident_a)
        link_incident(session, work_order_id=work_order.id, incident_id=incident_b)
        work_order_id = work_order.id

    with factory() as session:
        assert len(incidents_for_work_order(session, work_order_id=work_order_id)) == 2


def test_one_incident_can_have_more_than_one_work_order_over_its_life(factory) -> None:
    """If the first attempt did not fix it."""
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        asset = create_asset(session, canonical_name="Beta")
        session.flush()
        incident_id = seed_incident(session, asset_id=asset.id)
        first = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Tentativa 1",
            created_by="op", incident_ids=[incident_id],
        )
        second = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Tentativa 2",
            created_by="op", incident_ids=[incident_id],
        )
        ids = {first.id, second.id}

    with factory() as session:
        linked = {wo.id for wo in work_orders_for_incident(session, incident_id=incident_id)}
        assert linked == ids


def test_linking_the_same_pair_twice_does_not_duplicate(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        asset = create_asset(session, canonical_name="Beta")
        session.flush()
        incident_id = seed_incident(session, asset_id=asset.id)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op"
        )
        first = link_incident(session, work_order_id=work_order.id, incident_id=incident_id)
        second = link_incident(session, work_order_id=work_order.id, incident_id=incident_id)
        assert first.id == second.id
        work_order_id = work_order.id

    with factory() as session:
        assert len(incidents_for_work_order(session, work_order_id=work_order_id)) == 1


def test_linking_an_unknown_incident_is_refused(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op"
        )
        with pytest.raises(ValueError, match="Incidente desconhecido"):
            link_incident(session, work_order_id=work_order.id, incident_id=999999)


# --- validation ----------------------------------------------------------


def test_a_work_order_cannot_be_created_already_completed(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        with pytest.raises(ValueError, match="não pode nascer já concluído"):
            create_work_order(
                session, installation_id=installation_id, work_type="corrective", title="T",
                created_by="op", status="completed",
            )


def test_a_due_date_before_the_planned_date_is_refused(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        with pytest.raises(ValueError, match="data limite"):
            create_work_order(
                session, installation_id=installation_id, work_type="preventive", title="T", created_by="op",
                planned_date=date(2026, 6, 10), due_date=date(2026, 6, 1),
            )


def test_marking_completed_requires_and_stamps_completed_at(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="cleaning", title="Limpeza", created_by="op"
        )
        work_order_id = work_order.id

    with factory() as session, session.begin():
        updated = update_work_order_status(session, work_order_id=work_order_id, status="completed", actor="op")
        assert updated.completed_at is not None


def test_moving_off_completed_clears_completed_at(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="cleaning", title="Limpeza", created_by="op"
        )
        work_order_id = work_order.id
        update_work_order_status(session, work_order_id=work_order_id, status="completed", actor="op")

    with factory() as session, session.begin():
        reopened = update_work_order_status(session, work_order_id=work_order_id, status="in_progress", actor="op")
        assert reopened.completed_at is None


# --- visits ----------------------------------------------------------------


def test_a_work_order_can_have_several_visits(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op"
        )
        add_visit(session, work_order_id=work_order.id, visit_date=date(2026, 6, 1), created_by="op", technician="Anderson")
        add_visit(session, work_order_id=work_order.id, visit_date=date(2026, 6, 3), created_by="op", technician="Anderson")
        work_order_id = work_order.id

    with factory() as session:
        work_order = session.get(WorkOrder, work_order_id)
        assert [v.visit_date for v in work_order.visits] == [date(2026, 6, 1), date(2026, 6, 3)]


def test_deleting_a_work_order_cascades_its_visits_but_not_its_incidents(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        asset = create_asset(session, canonical_name="Beta")
        session.flush()
        incident_id = seed_incident(session, asset_id=asset.id)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op",
            incident_ids=[incident_id],
        )
        add_visit(session, work_order_id=work_order.id, visit_date=date(2026, 6, 1), created_by="op")
        work_order_id = work_order.id

    with factory() as session, session.begin():
        session.delete(session.get(WorkOrder, work_order_id))

    with factory() as session:
        assert session.scalar(select(func.count(Visit.id))) == 0
        assert session.get(DiagnosticIncident, incident_id) is not None


# --- planning queries --------------------------------------------------


def test_overdue_work_orders_excludes_completed_and_cancelled(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        overdue = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Atrasado", created_by="op",
            due_date=date.today() - timedelta(days=5),
        )
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Atrasado mas cancelado",
            created_by="op", due_date=date.today() - timedelta(days=5), status="cancelled",
        )
        overdue_id = overdue.id

    with factory() as session:
        results = overdue_work_orders(session)
        assert [wo.id for wo in results] == [overdue_id]


def test_unscheduled_work_orders_lists_only_open_work_with_no_date(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        undated = create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Sem data", created_by="op"
        )
        create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Com data", created_by="op",
            planned_date=date.today(),
        )
        undated_id = undated.id

    with factory() as session:
        results = unscheduled_work_orders(session)
        assert [wo.id for wo in results] == [undated_id]


def test_open_work_order_counts_covers_every_requested_installation(factory) -> None:
    with factory() as session, session.begin():
        with_work = seed_installation(session)
        without_work = seed_installation(session)
        create_work_order(
            session, installation_id=with_work, work_type="corrective", title="T", created_by="op"
        )

    with factory() as session:
        counts = open_work_order_counts(session, installation_ids=[with_work, without_work])
        assert counts == {with_work: 1, without_work: 0}


def test_work_orders_for_installation_orders_most_recently_planned_first(factory) -> None:
    with factory() as session, session.begin():
        installation_id = seed_installation(session)
        create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Primeiro", created_by="op",
            planned_date=date(2026, 6, 1),
        )
        create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Segundo", created_by="op",
            planned_date=date(2026, 6, 10),
        )

    with factory() as session:
        titles = [wo.title for wo in work_orders_for_installation(session, installation_id=installation_id)]
        assert titles == ["Segundo", "Primeiro"]
