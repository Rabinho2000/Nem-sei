from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def test_initial_migration_creates_foundation_tables(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    assert {
        "jobs", "job_events", "scheduler_leases", "schedule_state",
        "organizations", "assets", "asset_aliases", "devices", "provider_connections",
        "asset_provider_mappings", "legacy_import_runs", "legacy_import_records",
        "integration_health", "sync_runs", "sync_cursors", "provider_request_states",
        "provider_request_attempts", "asset_source_policies", "monitoring_observations", "monitoring_current_states", "production_facts",
        "operator_audit_events",
        "alembic_version",
    } <= set(inspect(engine).get_table_names())


def test_device_migration_downgrades_cleanly_when_no_devices_exist(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    assert "devices" in set(inspect(engine).get_table_names())
    command.downgrade(Config("alembic.ini"), "-1")
    remaining = set(inspect(create_engine(settings.database_url)).get_table_names())
    assert "devices" not in remaining
    assert {"assets", "asset_provider_mappings", "legacy_import_records"} <= remaining
    command.upgrade(Config("alembic.ini"), "head")
    assert "devices" in set(inspect(create_engine(settings.database_url)).get_table_names())


def test_device_migration_refuses_to_discard_existing_devices(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO assets (public_id, canonical_name, normalized_name, lifecycle_status, review_status, timezone_source, created_at, updated_at) "
                "VALUES ('asset-public-1', 'Alpha Solar', 'alpha solar', 'unknown', 'clear', 'manual', now(), now())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO devices (public_id, asset_id, device_kind, lifecycle_status, review_status, valid_from, created_at, updated_at) "
                "SELECT 'device-public-1', id, 'inverter', 'active', 'clear', CURRENT_DATE, now(), now() FROM assets LIMIT 1"
            )
        )
    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(Config("alembic.ini"), "-1")
    assert "devices" in set(inspect(create_engine(settings.database_url)).get_table_names())
