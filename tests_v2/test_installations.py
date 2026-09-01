"""`Installation`: the schema, the backfill, and what it does not touch.

The backfill is the load-bearing behaviour here. It has to be safe to run
twice, it has to leave every fact table alone, and it has to be the only
thing standing between "assets.installation_id exists" and "every asset has
one".
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from nemsei.assets.models import Asset
from nemsei.assets.service import create_asset, create_organization
from nemsei.db.session import build_session_factory
from nemsei.installations.models import Installation
from nemsei.installations.service import (
    backfill_installations_from_assets,
    coordinates_for_asset,
    installation_for_asset,
)
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.shared.clock import utc_now


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def factory_for(settings):
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return factory_for(settings)


# --- schema ---------------------------------------------------------------


def test_the_migration_creates_installations_and_a_nullable_link(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    tables = set(inspect(engine).get_table_names())
    assert "installations" in tables
    columns = {c["name"]: c for c in inspect(engine).get_columns("assets")}
    assert "installation_id" in columns
    assert columns["installation_id"]["nullable"] is True


def test_the_migration_downgrades_cleanly(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    command.downgrade(Config("alembic.ini"), "0031_installations-1")
    remaining = set(inspect(create_engine(settings.database_url)).get_table_names())
    assert "installations" not in remaining
    assert {"assets", "asset_provider_mappings", "production_facts"} <= remaining
    command.upgrade(Config("alembic.ini"), "head")


def test_a_coordinate_without_a_source_is_refused_by_the_database(factory) -> None:
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            now = utc_now()
            session.add(Installation(display_name="Alpha", created_at=now, updated_at=now))
            session.flush()
            session.execute(text("UPDATE installations SET latitude = 38.7, longitude = -9.1"))


def test_half_a_coordinate_pair_is_refused_by_the_database(factory) -> None:
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            now = utc_now()
            session.add(
                Installation(
                    display_name="Alpha",
                    coordinates_source="manual",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            session.execute(text("UPDATE installations SET latitude = 38.7"))


# --- the backfill -----------------------------------------------------------


def test_every_asset_without_one_gets_an_installation(factory) -> None:
    with factory() as session, session.begin():
        create_asset(session, canonical_name="Alpha")
        create_asset(session, canonical_name="Beta")

    with factory() as session, session.begin():
        result = backfill_installations_from_assets(session)

    assert result["created"] == 2 and result["already_linked"] == 0
    with factory() as session:
        assets = session.scalars(select(Asset)).all()
        assert all(asset.installation_id is not None for asset in assets)
        assert session.scalar(select(func.count(Installation.id))) == 2


def test_running_it_twice_creates_nothing_the_second_time(factory) -> None:
    with factory() as session, session.begin():
        create_asset(session, canonical_name="Alpha")

    with factory() as session, session.begin():
        first = backfill_installations_from_assets(session)
    with factory() as session, session.begin():
        second = backfill_installations_from_assets(session)

    assert first["created"] == 1
    assert second["created"] == 0 and second["already_linked"] == 1


def test_the_installation_carries_over_the_site_fields_not_the_technical_ones(factory) -> None:
    with factory() as session, session.begin():
        org = create_organization(session, display_name="DIACO")
        asset = create_asset(
            session,
            canonical_name="DIACO 1",
            owner_id=org.id,
            country_code="PT",
            address="Rua Principal 1",
            locality="Setúbal",
            timezone="Europe/Lisbon",
        )
        asset_id, org_id = asset.id, org.id

    with factory() as session, session.begin():
        backfill_installations_from_assets(session)

    with factory() as session:
        asset = session.get(Asset, asset_id)
        installation = session.get(Installation, asset.installation_id)
        assert installation.display_name == "DIACO 1"
        assert installation.organization_id == org_id
        assert installation.address == "Rua Principal 1"
        assert installation.locality == "Setúbal"
        assert installation.timezone == "Europe/Lisbon"


def test_a_new_asset_created_after_the_backfill_still_needs_it(factory) -> None:
    """The backfill is a step an operator re-runs, not a one-off migration
    trick -- a freshly created Asset has no Installation until it does."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Gamma")
        asset_id = asset.id

    with factory() as session:
        assert session.get(Asset, asset_id).installation_id is None


# --- what it must not touch -------------------------------------------------


def test_the_backfill_writes_no_provider_mapping_and_no_production_fact(factory) -> None:
    """The whole point of the incremental design: this milestone is schema
    plus a link, and ingestion is untouched."""
    with factory() as session, session.begin():
        create_asset(session, canonical_name="Alpha")

    with factory() as session, session.begin():
        backfill_installations_from_assets(session)

    with factory() as session:
        assert session.scalar(select(func.count(AssetProviderMapping.id))) == 0
        assert session.scalar(select(func.count(ProviderConnection.id))) == 0
        assert session.scalar(select(func.count(ProductionFact.id))) == 0


def test_deleting_an_installation_does_not_cascade_to_its_asset(factory) -> None:
    """ondelete=SET NULL, not CASCADE -- an Installation is administrative
    context for an Asset, never a precondition for the Asset existing."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session, session.begin():
        backfill_installations_from_assets(session)
        installation_id = session.get(Asset, asset_id).installation_id

    with factory() as session, session.begin():
        session.delete(session.get(Installation, installation_id))

    with factory() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        assert asset.installation_id is None


# --- the accessors used by monitoring ---------------------------------------


def test_coordinates_for_asset_is_none_none_before_the_backfill(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session:
        assert coordinates_for_asset(session, asset_id=asset_id) == (None, None)


def test_coordinates_for_asset_reads_through_the_installation(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session, session.begin():
        backfill_installations_from_assets(session)
        installation = installation_for_asset(session, asset_id=asset_id)
        installation.latitude = Decimal("38.7223")
        installation.longitude = Decimal("-9.1393")
        installation.coordinates_source = "manual"
        installation.coordinates_confidence = "manual"

    with factory() as session:
        latitude, longitude = coordinates_for_asset(session, asset_id=asset_id)
        assert latitude == Decimal("38.7223")
        assert longitude == Decimal("-9.1393")


def test_installation_for_unknown_asset_is_none(factory) -> None:
    with factory() as session:
        assert installation_for_asset(session, asset_id=999999) is None
