"""The diagnostic screens: worst-first, and honest about a device with no
reading at all."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.app import create_app
from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.service import record_device_status


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    return create_app(settings)


@pytest.fixture
def seeded(app, settings):
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Diagnostic Plant")
        healthy = create_device(
            session, asset_id=asset.id, device_kind="inverter", label="Healthy Inverter",
            model="SUN2000-60KTL", valid_from=date(2026, 1, 1),
        )
        record_device_status(
            session, device_id=healthy.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
            availability_status="available", active_power_kw=Decimal("12.5"), day_energy_kwh=Decimal("48.2"),
        )
        down = create_device(
            session, asset_id=asset.id, device_kind="inverter", label="Down Inverter",
            valid_from=date(2026, 1, 1),
        )
        record_device_status(
            session, device_id=down.id, asset_id=asset.id, source_fact_key="v1:2",
            observed_at=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
            availability_status="unavailable",
        )
        # A device that has never reported at all.
        create_device(session, asset_id=asset.id, device_kind="inverter", label="Silent Inverter", valid_from=date(2026, 1, 1))
        asset_id = asset.id
    return asset_id


def login(client) -> None:
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True
        browser_session["username"] = "ines"


def test_the_index_requires_authentication(app) -> None:
    assert app.test_client().get("/diagnostics").status_code in (302, 401)


def test_the_index_finds_the_installation_by_name(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get("/diagnostics?search=Diagnostic").get_data(as_text=True)
    assert "Diagnostic Plant" in body
    assert "3" in body  # device_count


def test_an_unknown_asset_is_404(app) -> None:
    client = app.test_client()
    login(client)
    assert client.get("/diagnostics/assets/999999").status_code == 404


def test_the_worst_device_is_listed_first(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/diagnostics/assets/{seeded}").get_data(as_text=True)
    down_index = body.index("Down Inverter")
    healthy_index = body.index("Healthy Inverter")
    assert down_index < healthy_index


def test_a_device_that_never_reported_is_shown_as_unknown_not_omitted(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/diagnostics/assets/{seeded}").get_data(as_text=True)
    assert "Silent Inverter" in body
    assert "sem histórico" in body


def test_the_counts_summarise_attention_correctly(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/diagnostics/assets/{seeded}").get_data(as_text=True)
    # 3 devices, 1 available, 2 need attention (unavailable + never reported), 1 with no reading at all.
    assert ">3<" in body
    assert ">1<" in body
    assert ">2<" in body


def test_findings_appear_on_the_asset_page_worst_first(app, seeded) -> None:
    """M7 Fatia 4 (docs/v2/DIAGNOSTICS.md): the deterministic rules engine,
    not just the raw device table, must actually reach the page."""
    client = app.test_client()
    login(client)
    body = client.get(f"/diagnostics/assets/{seeded}").get_data(as_text=True)
    assert "device_unavailable" in body
    assert "device_no_history" in body
    assert "partial_device_coverage" in body
    # Critical (Down Inverter, unavailable) must render before the
    # asset-level info finding (partial coverage).
    assert body.index("device_unavailable") < body.index("partial_device_coverage")


def test_an_asset_with_no_problems_says_so_plainly(app, settings) -> None:
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Quiet Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=datetime.now(timezone.utc), availability_status="available", active_power_kw=Decimal("5.0"),
        )
        asset_id = asset.id
    client = app.test_client()
    login(client)
    body = client.get(f"/diagnostics/assets/{asset_id}").get_data(as_text=True)
    assert "Nenhum problema detectado" in body
