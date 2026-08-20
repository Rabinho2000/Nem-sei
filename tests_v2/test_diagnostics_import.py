"""Importing V1's device_realtime_snapshots: idempotent, and honest about what
it cannot resolve.

Builds a small V1-shaped SQLite fixture rather than depending on the live V1
database, so this proves the importer's own logic — idempotency, decimal and
timezone handling, the skip-and-count behaviour for an unresolved device —
independently of whether real V1 evidence happens to be reachable from this
machine. `test_diagnostics_golden.py` is what compares against the real thing.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.repository import DeviceStatusRepository
from nemsei.diagnostics.v1_import import import_v1_device_status
from nemsei.providers.models import LegacyImportRecord, LegacyImportRun
from nemsei.shared.clock import utc_now


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def build_v1_fixture(path: Path, rows: list[dict]) -> None:
    """A minimal V1-shaped SQLite file: the tables open_v1_readonly requires,
    plus a real device_realtime_snapshots with the given rows."""
    connection = sqlite3.connect(path)
    try:
        for table in ("customers", "assets", "asset_aliases", "asset_integrations"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.execute(
            """
            CREATE TABLE device_realtime_snapshots (
                id INTEGER PRIMARY KEY,
                provider_device_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                station_code TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                inverter_state INTEGER,
                active_power_kw REAL,
                day_energy_kwh REAL,
                availability_status TEXT NOT NULL,
                communication_status TEXT NOT NULL
            )
            """
        )
        for row in rows:
            connection.execute(
                "INSERT INTO device_realtime_snapshots "
                "(id, provider_device_id, asset_id, provider, station_code, collected_at, "
                " inverter_state, active_power_kw, day_energy_kwh, availability_status, communication_status) "
                "VALUES (:id, :provider_device_id, :asset_id, 'FusionSolar', 'NE=1', :collected_at, "
                " :inverter_state, :active_power_kw, :day_energy_kwh, :availability_status, 'recent')",
                row,
            )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def asset_device_and_legacy_link(factory):
    """A V2 device already linked to a fake V1 provider_devices id 501, the
    same way M1's real identity import links every real device."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
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
                legacy_table="provider_devices",
                legacy_id="501",
                source_hash="0" * 64,
                outcome="created",
                target_device_id=device.id,
                created_at=utc_now(),
            )
        )
        ids = (asset.id, device.id)
    return factory, ids


def test_importing_creates_a_fact_per_row_and_skips_an_unresolved_device(asset_device_and_legacy_link, tmp_path) -> None:
    factory, (asset_id, device_id) = asset_device_and_legacy_link
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(
        v1_db,
        [
            # provider_device_id 501 is linked; 999 is not.
            {"id": 1, "provider_device_id": 501, "asset_id": 1, "collected_at": "2026-07-24T10:00:00",
             "inverter_state": 512, "active_power_kw": 4.2, "day_energy_kwh": 12.5, "availability_status": "available"},
            {"id": 2, "provider_device_id": 999, "asset_id": 1, "collected_at": "2026-07-24T11:00:00",
             "inverter_state": 512, "active_power_kw": 3.0, "day_energy_kwh": 9.0, "availability_status": "available"},
        ],
    )
    with factory() as session, session.begin():
        result = import_v1_device_status(session, v1_db)

    assert result["rows_read"] == 2
    assert result["facts_created"] == 1
    assert result["rows_without_device"] == 1

    with factory() as session:
        facts = DeviceStatusRepository(session).history_for_device(device_id=device_id)
    assert len(facts) == 1
    assert facts[0].availability_status == "available"
    assert float(facts[0].active_power_kw) == pytest.approx(4.2)
    assert facts[0].observed_at == datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    assert facts[0].metadata_json["v1_row_id"] == 1


def test_importing_the_same_file_twice_creates_nothing_the_second_time(asset_device_and_legacy_link, tmp_path) -> None:
    factory, (asset_id, device_id) = asset_device_and_legacy_link
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(
        v1_db,
        [
            {"id": 1, "provider_device_id": 501, "asset_id": 1, "collected_at": "2026-07-24T10:00:00",
             "inverter_state": 512, "active_power_kw": 4.2, "day_energy_kwh": 12.5, "availability_status": "available"},
        ],
    )
    with factory() as session, session.begin():
        first = import_v1_device_status(session, v1_db)
    with factory() as session, session.begin():
        second = import_v1_device_status(session, v1_db)

    assert first["facts_created"] == 1
    assert second["facts_created"] == 0
    assert second["facts_unchanged"] == 1

    with factory() as session:
        facts = DeviceStatusRepository(session).history_for_device(device_id=device_id)
    assert len(facts) == 1


def test_an_unrecognised_availability_status_is_counted_and_imported_as_unknown(asset_device_and_legacy_link, tmp_path) -> None:
    """V1's own vocabulary is trusted, but a row that breaks that trust must be
    visible in the manifest rather than silently coerced without a trace."""
    factory, (asset_id, device_id) = asset_device_and_legacy_link
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(
        v1_db,
        [
            {"id": 1, "provider_device_id": 501, "asset_id": 1, "collected_at": "2026-07-24T10:00:00",
             "inverter_state": 512, "active_power_kw": 4.2, "day_energy_kwh": 12.5, "availability_status": "connected"},
        ],
    )
    with factory() as session, session.begin():
        result = import_v1_device_status(session, v1_db)

    assert result["rows_with_unrecognised_status"] == 1
    with factory() as session:
        facts = DeviceStatusRepository(session).history_for_device(device_id=device_id)
    assert facts[0].availability_status == "unknown"


def test_a_negative_power_reading_is_dropped_rather_than_imported_as_a_negative(asset_device_and_legacy_link, tmp_path) -> None:
    factory, (asset_id, device_id) = asset_device_and_legacy_link
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(
        v1_db,
        [
            {"id": 1, "provider_device_id": 501, "asset_id": 1, "collected_at": "2026-07-24T10:00:00",
             "inverter_state": 512, "active_power_kw": -0.01, "day_energy_kwh": 12.5, "availability_status": "available"},
        ],
    )
    with factory() as session, session.begin():
        import_v1_device_status(session, v1_db)

    with factory() as session:
        facts = DeviceStatusRepository(session).history_for_device(device_id=device_id)
    assert facts[0].active_power_kw is None
    assert float(facts[0].day_energy_kwh) == pytest.approx(12.5)
