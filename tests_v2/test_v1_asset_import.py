from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select

from nemsei.assets.models import Asset
from nemsei.assets.v1_import import import_v1_assets, open_v1_readonly
from nemsei.db.session import build_session_factory
from nemsei.providers.models import AssetProviderMapping, LegacyImportRecord, ProviderConnection


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATA_ROOT", str(settings.data_root))
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def build_v1_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, nif TEXT, normalized_nif TEXT, active INTEGER, review_required INTEGER, review_notes TEXT);
            CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT NOT NULL, address TEXT, location TEXT, kwp TEXT, commissioning_date TEXT, country TEXT, timezone TEXT, notes TEXT, customer_id INTEGER);
            CREATE TABLE asset_aliases (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, alias_name TEXT NOT NULL, normalized_alias TEXT NOT NULL, source TEXT, active INTEGER);
            CREATE TABLE asset_integrations (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, provider TEXT NOT NULL, external_id TEXT, external_name TEXT, enabled INTEGER);
            INSERT INTO customers VALUES (1, 'Owner One', 'PT123456789', '123456789', 1, 1, 'Review owner');
            INSERT INTO assets VALUES (1, 'Alpha Solar', 'Road 1', 'Lisbon', '10.5', '2024-01-05', 'PT', NULL, 'legacy note', 1);
            INSERT INTO assets VALUES (2, 'Duplicate Name', NULL, NULL, NULL, NULL, 'PT', NULL, NULL, 1);
            INSERT INTO assets VALUES (3, ' duplicate-name ', NULL, NULL, NULL, NULL, 'PT', NULL, NULL, 1);
            INSERT INTO asset_aliases VALUES (1, 1, 'ALPHA-001', 'alpha001', 'legacy', 1);
            INSERT INTO asset_integrations VALUES (1, 1, 'FusionSolar', 'Plant-A', 'Alpha provider name', 1);
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_dry_run_is_read_only_and_accounts_for_eligible_rows(settings, tmp_path: Path) -> None:
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db)
    manifest = import_v1_assets(None, v1_db, dry_run=True)
    assert manifest["counts"]["customers.created"] == 1
    assert manifest["counts"]["assets.created"] == 1
    assert manifest["counts"]["assets.quarantined"] == 2
    assert manifest["counts"]["asset_aliases.created"] == 1
    assert manifest["counts"]["asset_integrations.created"] == 1
    connection = open_v1_readonly(v1_db)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM assets")
    assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 3
    connection.close()


def test_import_is_idempotent_and_preserves_changed_source(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db)
    engine = create_engine(settings.database_url)
    factory = build_session_factory(engine)
    with factory() as session, session.begin():
        first = import_v1_assets(session, v1_db)
    assert first["counts"]["assets.created"] == 1
    with factory() as session:
        asset = session.scalar(select(Asset).where(Asset.canonical_name == "Alpha Solar"))
        assert asset is not None
        assert asset.timezone_source == "migration_default"
        assert asset.review_status == "clear"
        assert session.scalar(select(func.count()).select_from(ProviderConnection)) == 1
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 1
        asset.canonical_name = "Manual V2 name"
        session.commit()
    connection = sqlite3.connect(v1_db)
    connection.execute("UPDATE assets SET project_name = 'Changed V1 source' WHERE id = 1")
    connection.commit()
    connection.close()
    with factory() as session, session.begin():
        rerun = import_v1_assets(session, v1_db)
    assert rerun["counts"]["assets.changed_source"] == 1
    with factory() as session:
        assert session.scalar(select(Asset).where(Asset.id == asset.id)).canonical_name == "Manual V2 name"
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(LegacyImportRecord).where(LegacyImportRecord.outcome == "changed_source")) >= 1


def test_rerun_without_source_change_does_not_duplicate_rows(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db)
    engine = create_engine(settings.database_url)
    factory = build_session_factory(engine)
    with factory() as session, session.begin():
        import_v1_assets(session, v1_db)
    with factory() as session, session.begin():
        manifest = import_v1_assets(session, v1_db)
    assert manifest["counts"]["assets.reused"] == 3
    assert manifest["counts"]["asset_integrations.reused"] == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 1
