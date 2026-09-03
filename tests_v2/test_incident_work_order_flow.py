"""The operational workflow this milestone closes: incidente -> análise ->
work order -> planeamento -> visita -> conclusão -> verificação do incidente.

Two lifecycles, proven independent in both directions here:
`WorkOrder.status` never resolves a `DiagnosticIncident`, and
`DiagnosticIncident.status` never closes a `WorkOrder` -- each moves only
through its own existing mechanism (`work_orders.service` /
`diagnostics.incidents`), exactly as before this milestone. What is new is
purely the navigation and the UI banners connecting the two, never a third
lifecycle grafted onto either.
"""
from __future__ import annotations

import contextlib
from datetime import date, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset
from nemsei.shared.clock import utc_now
from nemsei.web.diagnostics_queries import open_incidents_overview
from nemsei.web.work_order_queries import incident_side_banner, work_order_side_banner
from nemsei.work_orders.models import WorkOrder, WorkOrderIncident
from nemsei.work_orders.service import create_work_order, incidents_for_work_order, update_work_order_status


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def seeded(settings, monkeypatch, *, severity: str = "critical", rule_code: str = "plant_offline"):
    """One asset with an Installation already backfilled, and one open
    incident against it -- the starting point every scenario below shares:
    "incidente aberto", before an operator has done anything about it."""
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    asset = create_asset(session, canonical_name="Central Inversor Offline", timezone="Europe/Lisbon")
    session.flush()
    backfill_installations_from_assets(session)
    installation_id = installation_for_asset(session, asset_id=asset.id).id
    now = utc_now()
    incident = DiagnosticIncident(
        rule_code=rule_code, asset_id=asset.id, device_id=None, severity=severity, status="open",
        opened_at=now, last_observed_at=now, detector_version="test", created_at=now, updated_at=now,
    )
    session.add(incident)
    session.commit()
    incident_id, asset_id, installation_id = incident.id, asset.id, installation_id
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"], browser["csrf_token"] = True, "admin", "test"
    return client, incident_id, asset_id, installation_id


def reload_incident(settings, incident_id: int) -> DiagnosticIncident:
    session = build_session_factory(build_engine(settings))()
    try:
        incident = session.get(DiagnosticIncident, incident_id)
        session.expunge(incident)
        return incident
    finally:
        session.close()


def reload_work_order(settings, work_order_id: int) -> WorkOrder:
    session = build_session_factory(build_engine(settings))()
    try:
        work_order = session.get(WorkOrder, work_order_id)
        session.expunge(work_order)
        return work_order
    finally:
        session.close()


@contextlib.contextmanager
def count_queries(session):
    """How many statements a block issues against `session`'s own engine --
    the check `open_incidents_overview`'s batched work-order lookup exists
    to satisfy: query count must not grow with the number of incidents."""
    engine = session.get_bind()
    counter = {"n": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _before)


# --- 1: criar trabalho a partir de um incidente -----------------------------


def test_creating_a_work_order_from_an_incident_links_installation_incident_and_work_order(settings, monkeypatch) -> None:
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch)

    response = client.post(
        f"/diagnostics/incidents/{incident_id}/trabalho",
        data={
            "csrf_token": "test", "title": "Diagnóstico inversor offline", "work_type": "corrective",
            "priority": "critical", "assigned_to": "Anderson", "planned_date": "", "due_date": "",
            "material_status": "not_applicable",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/trabalhos/")

    session = build_session_factory(build_engine(settings))()
    work_order = session.scalar(select(WorkOrder))
    assert work_order is not None
    assert work_order.installation_id == installation_id
    assert work_order.priority == "critical"
    linked = incidents_for_work_order(session, work_order_id=work_order.id)
    assert [incident.id for incident in linked] == [incident_id]
    session.close()


def test_the_suggested_priority_follows_the_incident_severity_but_stays_overridable(settings, monkeypatch) -> None:
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch, severity="warning")

    page = client.get(f"/diagnostics/incidents/{incident_id}/trabalho/novo")
    assert page.status_code == 200
    # `warning` suggests `normal`, never `critical` -- see `PRIORITY_FROM_SEVERITY`.
    assert 'value="normal" selected' in page.text

    response = client.post(
        f"/diagnostics/incidents/{incident_id}/trabalho",
        data={"csrf_token": "test", "title": "T", "work_type": "corrective", "priority": "low"},
    )
    assert response.status_code == 302
    session = build_session_factory(build_engine(settings))()
    work_order = session.scalar(select(WorkOrder))
    assert work_order.priority == "low"  # the operator's override won, not the suggestion
    session.close()


def test_creating_a_work_order_does_not_duplicate_the_incident_link_even_if_the_same_incident_is_given_twice(
    settings, monkeypatch
) -> None:
    """The literal duplication `create_work_order`'s own `dict.fromkeys`
    guards against -- the boundary "criar novamente não duplica links"
    actually sits at."""
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op",
            incident_ids=[incident_id, incident_id],
        )
        work_order_id = work_order.id

    assert session.scalar(
        select(WorkOrderIncident).where(
            WorkOrderIncident.work_order_id == work_order_id, WorkOrderIncident.incident_id == incident_id
        )
    ) is not None
    count = len(list(session.scalars(select(WorkOrderIncident).where(WorkOrderIncident.work_order_id == work_order_id))))
    assert count == 1
    session.close()


def test_a_second_work_order_for_the_same_incident_is_a_distinct_row_never_a_merge(settings, monkeypatch) -> None:
    """Creating "outra vez" from the same incident is a deliberate secondary
    action (the first attempt did not fix it) -- a second `WorkOrder` row,
    and the join table gains one more pair, never a duplicate of the first."""
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch)
    for _ in range(2):
        response = client.post(
            f"/diagnostics/incidents/{incident_id}/trabalho",
            data={"csrf_token": "test", "title": "Tentativa", "work_type": "corrective", "priority": "normal"},
        )
        assert response.status_code == 302

    session = build_session_factory(build_engine(settings))()
    work_orders = list(session.scalars(select(WorkOrder)))
    assert len(work_orders) == 2
    links = list(session.scalars(select(WorkOrderIncident)))
    assert len(links) == 2
    assert {link.work_order_id for link in links} == {wo.id for wo in work_orders}
    session.close()


# --- 3/4: os dois lifecycles não se tocam -----------------------------------


def test_completing_a_work_order_does_not_resolve_its_incident(settings, monkeypatch) -> None:
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op",
            incident_ids=[incident_id],
        )
        work_order_id = work_order.id
    session.close()

    response = client.post(f"/trabalhos/{work_order_id}/estado", data={"csrf_token": "test", "status": "completed"})
    assert response.status_code == 302

    assert reload_work_order(settings, work_order_id).status == "completed"
    assert reload_incident(settings, incident_id).status == "open"  # untouched


def test_resolving_an_incident_does_not_close_its_work_order(settings, monkeypatch) -> None:
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op",
            incident_ids=[incident_id],
        )
        work_order_id = work_order.id

    # The mechanism the detector itself uses to resolve an incident
    # (`diagnostics.incidents._reconcile_asset_incidents`) -- exercised
    # directly here rather than re-run, since what this test proves is
    # `WorkOrder`'s reaction to that write, not the detector's own logic.
    with session.begin():
        incident = session.get(DiagnosticIncident, incident_id)
        incident.status = "resolved"
        incident.resolved_at = utc_now()

    assert reload_incident(settings, incident_id).status == "resolved"
    assert reload_work_order(settings, work_order_id).status == "open"  # untouched


def test_work_order_side_banner_flags_completed_work_against_an_unverified_incident() -> None:
    assert work_order_side_banner(work_order_status="completed", incident_statuses=["open"]) == (
        "Intervenção concluída; problema ainda não verificado como resolvido."
    )
    assert work_order_side_banner(work_order_status="completed", incident_statuses=["resolved"]) is None


def test_incident_side_banner_flags_recovered_monitoring_against_open_work() -> None:
    assert incident_side_banner(incident_status="resolved", work_order_statuses=["in_progress"]) == (
        "Monitorização já recuperou; trabalho continua aberto."
    )
    assert incident_side_banner(incident_status="resolved", work_order_statuses=["completed"]) is None
    assert incident_side_banner(incident_status="open", work_order_statuses=[]) is None


# --- 5: visita liga-se ao work order correto ---------------------------------


def test_a_visit_is_linked_to_the_correct_work_order(settings, monkeypatch) -> None:
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        first = create_work_order(session, installation_id=installation_id, work_type="corrective", title="A", created_by="op")
        second = create_work_order(session, installation_id=installation_id, work_type="corrective", title="B", created_by="op")
        first_id, second_id = first.id, second.id
    session.close()

    response = client.post(
        f"/trabalhos/{first_id}/visitas",
        data={"csrf_token": "test", "visit_date": "2026-09-10", "technician": "Anderson", "outcome": "Resolvido no local"},
    )
    assert response.status_code == 302

    session = build_session_factory(build_engine(settings))()
    assert len(session.get(WorkOrder, first_id).visits) == 1
    assert len(session.get(WorkOrder, second_id).visits) == 0
    session.close()


# --- 7: lista de incidentes mostra estado do trabalho sem N+1 ---------------


def test_incident_list_shows_open_work_order_state_without_query_count_growing_with_row_count(
    settings, monkeypatch
) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        asset = create_asset(session, canonical_name="Sozinho", timezone="Europe/Lisbon")
        session.flush()
        backfill_installations_from_assets(session)
        now = utc_now()
        incident = DiagnosticIncident(
            rule_code="plant_offline", asset_id=asset.id, severity="critical", status="open",
            opened_at=now, last_observed_at=now, detector_version="t", created_at=now, updated_at=now,
        )
        session.add(incident)
    with count_queries(session) as counter:
        open_incidents_overview(session, om="todos")
    baseline = counter["n"]
    session.rollback()  # close the read-only transaction the query above autobegan

    with session.begin():
        for i in range(5):
            asset = create_asset(session, canonical_name=f"Central {i}", timezone="Europe/Lisbon")
            session.flush()
            backfill_installations_from_assets(session)
            other_installation_id = installation_for_asset(session, asset_id=asset.id).id
            now = utc_now()
            other_incident = DiagnosticIncident(
                rule_code="plant_offline", asset_id=asset.id, severity="critical", status="open",
                opened_at=now, last_observed_at=now, detector_version="t", created_at=now, updated_at=now,
            )
            session.add(other_incident)
            session.flush()
            if i % 2 == 0:
                create_work_order(
                    session, installation_id=other_installation_id, work_type="corrective", title=f"WO {i}",
                    created_by="op", incident_ids=[other_incident.id],
                )

    with count_queries(session) as counter:
        rows = open_incidents_overview(session, om="todos")
    assert len(rows) == 6
    # Batched: the query count for 6 incidents (3 with open work) is the same
    # as for 1 -- never one extra round-trip per row.
    assert counter["n"] == baseline
    session.close()


# --- 8: fluxo completo, ao nível do browser ---------------------------------


def test_full_operational_flow_incident_to_work_order_to_visit_to_completion(settings, monkeypatch) -> None:
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch, severity="critical")

    # 1. abrir incidente
    incident_page = client.get(f"/diagnostics/incidents/{incident_id}")
    assert incident_page.status_code == 200
    assert "Criar trabalho" in incident_page.text

    # 2. criar trabalho
    new_form = client.get(f"/diagnostics/incidents/{incident_id}/trabalho/novo")
    assert new_form.status_code == 200
    created = client.post(
        f"/diagnostics/incidents/{incident_id}/trabalho",
        data={
            "csrf_token": "test", "title": "Diagnóstico inversor offline", "work_type": "corrective",
            "priority": "critical", "assigned_to": "Anderson", "planned_date": (date.today() + timedelta(days=1)).isoformat(),
        },
    )
    assert created.status_code == 302
    work_order_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[-1])

    # 3. abrir trabalho
    detail_page = client.get(f"/trabalhos/{work_order_id}")
    assert detail_page.status_code == 200
    assert "Diagnóstico inversor offline" in detail_page.text
    assert "Não concluído" in detail_page.text

    # 4. registar visita
    visit_response = client.post(
        f"/trabalhos/{work_order_id}/visitas",
        data={"csrf_token": "test", "visit_date": date.today().isoformat(), "technician": "Anderson", "outcome": "Resolvido no local"},
    )
    assert visit_response.status_code == 302
    after_visit = client.get(f"/trabalhos/{work_order_id}")
    assert "Anderson" in after_visit.text

    # 5. concluir o trabalho
    complete_response = client.post(f"/trabalhos/{work_order_id}/estado", data={"csrf_token": "test", "status": "completed"})
    assert complete_response.status_code == 302

    # 6. UI continua a mostrar o incidente ativo -- o detetor não recuperou ainda
    completed_page = client.get(f"/trabalhos/{work_order_id}")
    assert "Concluído" in completed_page.text
    assert "Intervenção concluída; problema ainda não verificado como resolvido." in completed_page.text
    incident_after_completion = client.get(f"/diagnostics/incidents/{incident_id}")
    assert reload_incident(settings, incident_id).status == "open"
    assert "Intervenção concluída; problema ainda não verificado como resolvido." in incident_after_completion.text

    # 7/8. diagnostics recebe um estado saudável -- o incidente resolve pelo
    # mecanismo já existente (o detetor, não este trabalho).
    session = build_session_factory(build_engine(settings))()
    with session.begin():
        incident = session.get(DiagnosticIncident, incident_id)
        incident.status = "resolved"
        incident.resolved_at = utc_now()
    session.close()

    # 9. histórico mantém todo o percurso: o trabalho continua concluído, a
    # visita continua lá, o incidente está resolvido -- lifecycles independentes.
    final_incident_page = client.get(f"/diagnostics/incidents/{incident_id}")
    assert "resolvido" in final_incident_page.text.lower()
    final_work_order = reload_work_order(settings, work_order_id)
    assert final_work_order.status == "completed"
    session = build_session_factory(build_engine(settings))()
    assert len(session.get(WorkOrder, work_order_id).visits) == 1
    session.close()


def test_a_work_order_still_open_when_its_incident_resolves_shows_the_banner_on_both_pages(settings, monkeypatch) -> None:
    client, incident_id, asset_id, installation_id = seeded(settings, monkeypatch)
    created = client.post(
        f"/diagnostics/incidents/{incident_id}/trabalho",
        data={"csrf_token": "test", "title": "T", "work_type": "corrective", "priority": "normal"},
    )
    work_order_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[-1])

    session = build_session_factory(build_engine(settings))()
    with session.begin():
        incident = session.get(DiagnosticIncident, incident_id)
        incident.status = "resolved"
        incident.resolved_at = utc_now()
    session.close()

    incident_page = client.get(f"/diagnostics/incidents/{incident_id}")
    assert "Monitorização já recuperou; trabalho continua aberto." in incident_page.text
    work_order_page = client.get(f"/trabalhos/{work_order_id}")
    assert "Monitorização já recuperou; trabalho continua aberto." in work_order_page.text


# --- 9: migrations up/down ---------------------------------------------------


def _seed_installation(session) -> int:
    asset = create_asset(session, canonical_name="Downgrade")
    session.flush()
    backfill_installations_from_assets(session)
    return installation_for_asset(session, asset_id=asset.id).id


def test_the_priority_and_new_statuses_migration_downgrades_cleanly(settings, monkeypatch, factory) -> None:
    columns = {c["name"] for c in inspect(create_engine(settings.database_url)).get_columns("work_orders")}
    assert {"priority", "updated_by"} <= columns

    # A work order in one of the two new states, to prove the downgrade
    # normalises it rather than leaving an orphaned status value behind.
    with factory() as session, session.begin():
        installation_id = _seed_installation(session)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="T", created_by="op",
        )
        update_work_order_status(session, work_order_id=work_order.id, status="waiting_material", actor="op")
        work_order_id = work_order.id

    command.downgrade(Config("alembic.ini"), "0038_work_order_priority-1")
    try:
        columns = {c["name"] for c in inspect(create_engine(settings.database_url)).get_columns("work_orders")}
        assert not ({"priority", "updated_by"} & columns)
        with factory() as session:
            row = session.execute(text("SELECT status FROM work_orders WHERE id = :id"), {"id": work_order_id}).scalar_one()
            assert row == "open"  # normalised down from waiting_material, not left dangling

        with pytest.raises(IntegrityError):
            with factory() as session, session.begin():
                session.execute(
                    text(
                        "INSERT INTO work_orders (public_id, installation_id, work_type, status, title,"
                        " material_status, created_by, created_at, updated_at)"
                        " VALUES ('y', :installation_id, 'corrective', 'waiting_material', 't', 'not_applicable', 'op', now(), now())"
                    ),
                    {"installation_id": installation_id},
                )
    finally:
        command.upgrade(Config("alembic.ini"), "head")
