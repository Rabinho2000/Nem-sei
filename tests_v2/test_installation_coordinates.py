"""Coordinates onto `Installation`, and the constraints that keep them honest.

Built on a V1-shaped SQLite fixture rather than the live V1 database, so this
proves the importer's own behaviour on a machine that cannot reach V1.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.installations.coordinates_import import import_v1_coordinates, parse_coordinate
from nemsei.installations.models import Installation
from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset
from nemsei.monitoring.production_window import window_for
from nemsei.providers.models import LegacyImportRecord, LegacyImportRun
from nemsei.shared.clock import utc_now


LATITUDE_BOUNDS = (Decimal("-90"), Decimal("90"))
LONGITUDE_BOUNDS = (Decimal("-180"), Decimal("180"))


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def build_v1_fixture(path: Path, rows: list[dict]) -> None:
    connection = sqlite3.connect(path)
    try:
        for table in ("customers", "asset_aliases", "asset_integrations"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT, latitude REAL,"
            " longitude REAL, coordinates_source TEXT, coordinates_confidence TEXT)"
        )
        for row in rows:
            connection.execute(
                "INSERT INTO assets (id, project_name, latitude, longitude, coordinates_source,"
                " coordinates_confidence) VALUES (:id, :project_name, :latitude, :longitude,"
                " :coordinates_source, :coordinates_confidence)",
                row,
            )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def link(session, *, legacy_id: str, asset_id: int) -> None:
    run = LegacyImportRun(
        source_database_sha256="0" * 64,
        source_locator_sha256="0" * 64,
        importer_version="test",
        dry_run=False,
        started_at=utc_now(),
        manifest_json={},
    )
    session.add(run)
    session.flush()
    session.add(
        LegacyImportRecord(
            import_run_id=run.id,
            source_database_sha256="0" * 64,
            source_locator_sha256="0" * 64,
            legacy_table="assets",
            legacy_id=legacy_id,
            source_hash="0" * 64,
            outcome="created",
            target_asset_id=asset_id,
            created_at=utc_now(),
        )
    )


def seed(session, *, legacy_id: str, canonical_name: str, backfill: bool = True) -> int:
    asset = create_asset(session, canonical_name=canonical_name)
    link(session, legacy_id=legacy_id, asset_id=asset.id)
    if backfill:
        session.flush()
        backfill_installations_from_assets(session)
    return asset.id


# --- the parser ---------------------------------------------------------


def test_a_coordinate_outside_the_globe_is_refused() -> None:
    assert parse_coordinate("91", bounds=LATITUDE_BOUNDS) is None
    assert parse_coordinate("-90.1", bounds=LATITUDE_BOUNDS) is None
    assert parse_coordinate("181", bounds=LONGITUDE_BOUNDS) is None


def test_unparseable_text_is_none_rather_than_zero() -> None:
    """Zero is a real place. "" and "n/a" must never become it."""
    for value in ("", "   ", "n/a", None, "38,7223"):
        assert parse_coordinate(value, bounds=LATITUDE_BOUNDS) is None


def test_precision_is_clipped_to_what_the_column_stores() -> None:
    assert parse_coordinate("45.0570150084594", bounds=LATITUDE_BOUNDS) == Decimal("45.0570150")


# --- the import ---------------------------------------------------------


def test_coordinates_and_their_provenance_arrive_on_the_installation(factory, tmp_path) -> None:
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1849, "project_name": "A Colmeia do Minho", "latitude": 38.6112781,
                           "longitude": -9.0759184, "coordinates_source": "google_mymaps",
                           "coordinates_confidence": "ok"}])
    with factory() as session, session.begin():
        asset_id = seed(session, legacy_id="1849", canonical_name="A Colmeia do Minho")

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["imported"] == 1
    with factory() as session:
        installation = installation_for_asset(session, asset_id=asset_id)
        assert installation.latitude == Decimal("38.6112781")
        assert installation.longitude == Decimal("-9.0759184")
        assert installation.coordinates_source == "google_mymaps"
        assert installation.coordinates_confidence == "ok"


def test_a_suspect_geocode_keeps_saying_it_is_suspect(factory, tmp_path) -> None:
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1891, "project_name": "Granitos", "latitude": 42.064136,
                           "longitude": -8.498336, "coordinates_source": "openrouteservice",
                           "coordinates_confidence": "suspect"}])
    with factory() as session, session.begin():
        asset_id = seed(session, legacy_id="1891", canonical_name="Granitos")

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["by_confidence"] == {"suspect": 1}
    with factory() as session:
        assert installation_for_asset(session, asset_id=asset_id).coordinates_confidence == "suspect"


def test_an_asset_with_no_installation_yet_is_reported_not_skipped(factory, tmp_path) -> None:
    """The backfill has to run before this. An Asset caught mid-transition
    must be visible in the summary, not silently dropped."""
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 38.7, "longitude": -9.1,
                           "coordinates_source": "manual", "coordinates_confidence": "manual"}])
    with factory() as session, session.begin():
        seed(session, legacy_id="1", canonical_name="Alpha", backfill=False)

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["imported"] == 0
    assert result["no_installation"] == 1


def test_running_it_twice_changes_nothing(factory, tmp_path) -> None:
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 38.7, "longitude": -9.1,
                           "coordinates_source": "manual", "coordinates_confidence": "manual"}])
    with factory() as session, session.begin():
        seed(session, legacy_id="1", canonical_name="Alpha")

    with factory() as session, session.begin():
        first = import_v1_coordinates(session, v1)
    with factory() as session, session.begin():
        second = import_v1_coordinates(session, v1)

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["already_present"] == 1


def test_an_operator_value_is_never_overwritten_by_the_import(factory, tmp_path) -> None:
    """A bulk import must not outrank a person who typed a coordinate in."""
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 10.0, "longitude": 10.0,
                           "coordinates_source": "openrouteservice", "coordinates_confidence": "suspect"}])
    with factory() as session, session.begin():
        asset_id = seed(session, legacy_id="1", canonical_name="Alpha")
        installation = installation_for_asset(session, asset_id=asset_id)
        installation.latitude = Decimal("38.7000000")
        installation.longitude = Decimal("-9.1000000")
        installation.coordinates_source = "operator"
        installation.coordinates_confidence = "manual"

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["imported"] == 0 and result["already_present"] == 1
    with factory() as session:
        assert installation_for_asset(session, asset_id=asset_id).coordinates_source == "operator"


def test_a_dry_run_reports_without_writing(factory, tmp_path) -> None:
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 38.7, "longitude": -9.1,
                           "coordinates_source": "manual", "coordinates_confidence": "manual"}])
    with factory() as session, session.begin():
        asset_id = seed(session, legacy_id="1", canonical_name="Alpha")

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1, dry_run=True)

    assert result["imported"] == 1 and result["dry_run"] is True
    with factory() as session:
        assert installation_for_asset(session, asset_id=asset_id).latitude is None


def test_null_island_is_rejected_with_its_reason_recorded(factory, tmp_path) -> None:
    """(0, 0) is what a failed geocode returns, not a Solcor plant."""
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 0.0, "longitude": 0.0,
                           "coordinates_source": "openrouteservice", "coordinates_confidence": "suspect"}])
    with factory() as session, session.begin():
        asset_id = seed(session, legacy_id="1", canonical_name="Alpha")

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["imported"] == 0
    assert len(result["rejected"]) == 1
    assert "0, 0" in result["rejected"][0]["reason"]
    with factory() as session:
        assert installation_for_asset(session, asset_id=asset_id).latitude is None


def test_a_pair_with_no_recorded_origin_is_refused_not_invented(factory, tmp_path) -> None:
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 38.7, "longitude": -9.1,
                           "coordinates_source": None, "coordinates_confidence": None}])
    with factory() as session, session.begin():
        seed(session, legacy_id="1", canonical_name="Alpha")

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["imported"] == 0
    assert "origem desconhecida" in result["rejected"][0]["reason"]


def test_a_v1_plant_that_never_became_an_installation_is_counted_not_dropped(factory, tmp_path) -> None:
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 4242, "project_name": "Quarantined", "latitude": 38.7, "longitude": -9.1,
                           "coordinates_source": "manual", "coordinates_confidence": "manual"}])
    with factory() as session, session.begin():
        create_asset(session, canonical_name="Unrelated")

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["considered"] == 1 and result["unlinked"] == 1 and result["imported"] == 0


def test_installations_v1_could_not_place_either_are_reported_as_the_real_gap(factory, tmp_path) -> None:
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 38.7, "longitude": -9.1,
                           "coordinates_source": "manual", "coordinates_confidence": "manual"}])
    with factory() as session, session.begin():
        seed(session, legacy_id="1", canonical_name="Alpha")
        create_asset(session, canonical_name="Nowhere One")
        create_asset(session, canonical_name="Nowhere Two")
        backfill_installations_from_assets(session)

    with factory() as session, session.begin():
        result = import_v1_coordinates(session, v1)

    assert result["imported"] == 1
    assert result["absent_in_v1"] == 2


# --- the constraints ----------------------------------------------------


def test_an_unknown_coordinate_source_is_refused_by_the_database(factory) -> None:
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            now = utc_now()
            installation = Installation(display_name="Alpha", created_at=now, updated_at=now)
            session.add(installation)
            session.flush()
            session.execute(
                text(
                    "UPDATE installations SET latitude = 38.7, longitude = -9.1,"
                    " coordinates_source = 'guessed' WHERE id = :id"
                ),
                {"id": installation.id},
            )


# --- what it unlocks ----------------------------------------------------


def test_an_imported_installation_can_finally_answer_the_production_window(factory, tmp_path) -> None:
    """The point of the whole block: before this, every installation answered
    `unknown` around the clock."""
    v1 = tmp_path / "v1.db"
    build_v1_fixture(v1, [{"id": 1, "project_name": "Alpha", "latitude": 38.7223, "longitude": -9.1393,
                           "coordinates_source": "google_mymaps", "coordinates_confidence": "ok"}])
    with factory() as session, session.begin():
        asset_id = seed(session, legacy_id="1", canonical_name="Alpha")

    with factory() as session, session.begin():
        import_v1_coordinates(session, v1)

    with factory() as session:
        installation = installation_for_asset(session, asset_id=asset_id)
        noon = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
        night = datetime(2026, 6, 21, 3, 0, tzinfo=timezone.utc)
        assert window_for(latitude=installation.latitude, longitude=installation.longitude, at=noon).state == "productive"
        assert window_for(latitude=installation.latitude, longitude=installation.longitude, at=night).state == "dark"
