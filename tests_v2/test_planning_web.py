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
from nemsei.work_orders.service import create_work_order


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
