from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def test_initial_migration_creates_foundation_tables(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    assert {
        "jobs", "job_events", "scheduler_leases", "schedule_state",
        "organizations", "assets", "asset_aliases", "provider_connections",
        "asset_provider_mappings", "legacy_import_runs", "legacy_import_records",
        "integration_health", "sync_runs", "sync_cursors", "provider_request_states",
        "provider_request_attempts", "asset_source_policies", "monitoring_observations", "production_facts",
        "alembic_version",
    } <= set(inspect(engine).get_table_names())
