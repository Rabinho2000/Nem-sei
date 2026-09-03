"""Planeamento: the dashboard's work-order buckets (esta semana / atrasados
/ bloqueados / sem data / próximos) as their own screen -- independent
questions about the same open work, not a partition.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset
from nemsei.shared.clock import utc_now
from nemsei.web.work_order_queries import planning_page
from nemsei.work_orders.service import create_work_order, update_work_order_status


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def login(client) -> None:
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"


def _installation_id(session, *, asset_id: int) -> int:
    backfill_installations_from_assets(session)
    return installation_for_asset(session, asset_id=asset_id).id


def test_planning_buckets_split_open_work_orders_by_what_they_need(factory) -> None:
    today = utc_now().date()
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)

        this_week = create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Esta semana",
            created_by="op", planned_date=today + timedelta(days=2),
        )
        overdue = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Atrasado",
            created_by="op", planned_date=today - timedelta(days=5), due_date=today - timedelta(days=1),
        )
        blocked = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Bloqueado",
            created_by="op", material_status="pending",
        )
        no_date = create_work_order(
            session, installation_id=installation_id, work_type="cleaning", title="Sem data", created_by="op",
        )
        later = create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Mais tarde",
            created_by="op", planned_date=today + timedelta(days=30),
        )
        ids = {
            "this_week": this_week.id, "overdue": overdue.id, "blocked": blocked.id,
            "no_date": no_date.id, "later": later.id,
        }

    with factory() as session:
        page = planning_page(session)

    assert [row["work_order"].id for row in page["esta_semana"]] == [ids["this_week"]]
    assert [row["work_order"].id for row in page["atrasados"]] == [ids["overdue"]]
    assert [row["work_order"].id for row in page["bloqueados"]] == [ids["blocked"]]
    assert [row["work_order"].id for row in page["sem_data"]] == [ids["blocked"], ids["no_date"]]
    assert [row["work_order"].id for row in page["proximos"]] == [ids["later"]]
    assert page["total_open"] == 5


def test_a_work_order_can_appear_in_more_than_one_bucket(factory) -> None:
    """Overdue and blocked on material at once: hiding it from one bucket
    because it already showed up in the other would lose exactly the fact
    that explains why the job is still stuck."""
    today = utc_now().date()
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)
        work_order = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Preso",
            created_by="op", due_date=today - timedelta(days=3), material_status="ordered",
        )
        work_order_id = work_order.id

    with factory() as session:
        page = planning_page(session)

    assert [row["work_order"].id for row in page["atrasados"]] == [work_order_id]
    assert [row["work_order"].id for row in page["bloqueados"]] == [work_order_id]


def test_planning_excludes_completed_and_cancelled_work(factory) -> None:
    today = utc_now().date()
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Cancelado",
            created_by="op", status="cancelled", due_date=today - timedelta(days=10),
        )

    with factory() as session:
        page = planning_page(session)

    assert page["total_open"] == 0
    assert page["atrasados"] == []


def test_the_planning_page_renders(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    session = build_session_factory(create_engine(settings.database_url))()
    today = utc_now().date()
    with session.begin():
        asset = create_asset(session, canonical_name="Central Planeada")
        installation_id = _installation_id(session, asset_id=asset.id)
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Trocar disjuntor",
            created_by="op", due_date=today - timedelta(days=1),
        )
    session.close()

    client = create_app(settings).test_client()
    login(client)
    response = client.get("/planeamento")
    assert response.status_code == 200
    assert "Planeamento" in response.text
    assert "Trocar disjuntor" in response.text


def test_criticos_bucket_holds_only_open_critical_priority_work(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)
        critical = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Crítico",
            created_by="op", priority="critical",
        )
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Normal",
            created_by="op", priority="normal",
        )
        critical_id = critical.id

    with factory() as session:
        page = planning_page(session)

    assert [row["work_order"].id for row in page["criticos"]] == [critical_id]


def test_hoje_bucket_holds_only_work_planned_for_today(factory) -> None:
    today = utc_now().date()
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)
        today_wo = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Hoje",
            created_by="op", planned_date=today,
        )
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Amanhã",
            created_by="op", planned_date=today + timedelta(days=1),
        )
        today_id = today_wo.id

    with factory() as session:
        page = planning_page(session)

    assert [row["work_order"].id for row in page["hoje"]] == [today_id]


def test_bloqueados_bucket_also_holds_the_explicit_waiting_material_status(factory) -> None:
    """`material_status='pending'/'ordered'` and `status='waiting_material'`
    are two different facts (see `work_orders/models.py`) that both belong
    in the same "à espera de material" bucket."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)
        by_material_status = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Material pendente",
            created_by="op", material_status="pending",
        )
        by_status = create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Parado no estado",
            created_by="op",
        )
        update_work_order_status(session, work_order_id=by_status.id, status="waiting_material", actor="op")
        by_material_status_id, by_status_id = by_material_status.id, by_status.id

    with factory() as session:
        page = planning_page(session)

    assert {row["work_order"].id for row in page["bloqueados"]} == {by_material_status_id, by_status_id}


def test_concluidos_recentes_holds_only_recently_completed_work(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)
        recent = create_work_order(
            session, installation_id=installation_id, work_type="cleaning", title="Feito ontem", created_by="op"
        )
        update_work_order_status(session, work_order_id=recent.id, status="completed", actor="op")
        still_open = create_work_order(
            session, installation_id=installation_id, work_type="cleaning", title="Ainda aberto", created_by="op"
        )
        recent_id, still_open_id = recent.id, still_open.id

    with factory() as session:
        page = planning_page(session)

    completed_ids = {row["work_order"].id for row in page["concluidos_recentes"]}
    assert recent_id in completed_ids
    assert still_open_id not in completed_ids
    open_ids = {row["work_order"].id for row in page["sem_data"]}
    assert recent_id not in open_ids  # completed work never appears in an "open work" bucket


def test_planning_filters_narrow_every_bucket_identically(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central")
        installation_id = _installation_id(session, asset_id=asset.id)
        matching = create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Filtra",
            created_by="op", priority="high", assigned_to="Anderson",
        )
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Não filtra",
            created_by="op", priority="low", assigned_to="Outra Pessoa",
        )
        matching_id = matching.id

    with factory() as session:
        by_priority = planning_page(session, priority="high")
        by_work_type = planning_page(session, work_type="preventive")
        by_assigned_to = planning_page(session, assigned_to="Ander")

    for page in (by_priority, by_work_type, by_assigned_to):
        assert [row["work_order"].id for row in page["sem_data"]] == [matching_id]
        assert page["total_open"] == 1


def test_planning_installation_filter_isolates_one_site(factory) -> None:
    with factory() as session, session.begin():
        alpha = create_asset(session, canonical_name="Central Alpha")
        alpha_installation_id = _installation_id(session, asset_id=alpha.id)
        beta = create_asset(session, canonical_name="Central Beta")
        beta_installation_id = _installation_id(session, asset_id=beta.id)
        alpha_wo = create_work_order(
            session, installation_id=alpha_installation_id, work_type="corrective", title="Em Alpha", created_by="op"
        )
        create_work_order(
            session, installation_id=beta_installation_id, work_type="corrective", title="Em Beta", created_by="op"
        )
        alpha_id = alpha_wo.id

    with factory() as session:
        page = planning_page(session, installation="Alpha")

    assert [row["work_order"].id for row in page["sem_data"]] == [alpha_id]
    assert page["total_open"] == 1
