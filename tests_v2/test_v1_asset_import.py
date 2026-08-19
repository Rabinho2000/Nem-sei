from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select

from nemsei.assets import v1_import as v1_import_module
from nemsei.assets.models import Asset, AssetAlias, Device
from nemsei.assets.v1_import import import_v1_assets, open_v1_readonly
from nemsei.db.session import build_session_factory
from nemsei.providers.models import AssetProviderMapping, LegacyImportRecord, LegacyImportRun, ProviderConnection


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def build_v1_fixture(path: Path, *, first_asset_country: str = "PT", with_devices: bool = False) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, nif TEXT, normalized_nif TEXT, active INTEGER, review_required INTEGER, review_notes TEXT);
            CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT NOT NULL, address TEXT, location TEXT, kwp TEXT, commissioning_date TEXT, country TEXT, timezone TEXT, notes TEXT, customer_id INTEGER);
            CREATE TABLE asset_aliases (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, alias_name TEXT NOT NULL, normalized_alias TEXT NOT NULL, source TEXT, active INTEGER);
            CREATE TABLE asset_integrations (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, provider TEXT NOT NULL, external_id TEXT, external_name TEXT, enabled INTEGER);
            CREATE TABLE integration_unresolved (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT, external_name TEXT NOT NULL, normalized_name TEXT NOT NULL, external_status TEXT, resolution_status TEXT);
            INSERT INTO customers VALUES (1, 'Owner One', 'PT123456789', '123456789', 1, 1, 'Review owner');
            INSERT INTO assets VALUES (1, 'Alpha Solar', 'Road 1', 'Lisbon', '10.5', '2024-01-05', 'PT', NULL, 'legacy note', 1);
            INSERT INTO assets VALUES (2, 'Duplicate Name', NULL, NULL, NULL, NULL, 'PT', NULL, NULL, 1);
            INSERT INTO assets VALUES (3, ' duplicate-name ', NULL, NULL, NULL, NULL, 'PT', NULL, NULL, 1);
            INSERT INTO asset_aliases VALUES (1, 1, 'ALPHA-001', 'alpha001', 'legacy', 1);
            INSERT INTO asset_integrations VALUES (1, 1, 'FusionSolar', 'Plant-A', 'Alpha provider name', 1);
            INSERT INTO integration_unresolved VALUES (1, 'FusionSolar', 'unknown-1', 'Unknown plant', 'unknown plant', NULL, 'pending');
            """
        )
        connection.execute("UPDATE assets SET country = ? WHERE id = 1", (first_asset_country,))
        if with_devices:
            # Device 1 is importable, device 2 has an unmapped V1 type, and
            # device 3 hangs off an asset the identity import quarantines.
            connection.executescript(
                """
                CREATE TABLE provider_devices (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, provider TEXT NOT NULL, station_code TEXT, external_device_id TEXT, dev_dn TEXT, sn TEXT, device_name TEXT, model TEXT, rated_power_kw REAL, dev_type_id INTEGER, enabled INTEGER);
                INSERT INTO provider_devices VALUES (1, 1, 'FusionSolar', 'Plant-A', '1000000139150452', 'NE=139150452', '6T2159042269', 'Inverter 1', 'SUN2000-30KTL-M3', 30.0, 1, 1);
                INSERT INTO provider_devices VALUES (2, 1, 'FusionSolar', 'Plant-A', '1000000139150454', 'NE=139150454', '6T2159040909', 'Inverter 2', 'SUN2000-30KTL-M3', 30.0, 99, 1);
                INSERT INTO provider_devices VALUES (3, 2, 'FusionSolar', 'Plant-B', '1000000139150456', 'NE=139150456', 'ES2330055991', 'Inverter 3', 'SUN2000-50KTL-M3', 50.0, 1, 0);
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
        assert asset.timezone is None
        assert asset.timezone_source == "unknown"
        assert asset.country_code == "PT"
        assert asset.review_status == "needs_review"
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


def test_import_reports_connection_scoped_mapping_collision(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db)
    source = sqlite3.connect(v1_db)
    source.executescript(
        """
        INSERT INTO assets VALUES (4, 'Bravo Solar', NULL, 'Porto', '5', NULL, 'PT', NULL, NULL, NULL);
        INSERT INTO asset_integrations VALUES (2, 4, 'FusionSolar', 'Plant-A', 'Collision', 1);
        """
    )
    source.commit()
    source.close()
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        manifest = import_v1_assets(session, v1_db)
    assert manifest["counts"]["asset_integrations.conflict"] == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 1


def test_source_fingerprint_prevents_cross_database_id_reuse_and_records_unresolved(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    build_v1_fixture(first)
    build_v1_fixture(second)
    source = sqlite3.connect(second)
    source.execute("UPDATE assets SET project_name = 'Different source asset' WHERE id = 1")
    source.commit()
    source.close()
    factory = build_session_factory(create_engine(settings.database_url))
    for path in (first, second):
        with factory() as session, session.begin():
            manifest = import_v1_assets(session, path)
            assert manifest["counts"]["integration_unresolved.unresolved"] == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 2
        assert session.scalar(select(func.count()).select_from(LegacyImportRun)) == 2
        assert session.scalar(select(LegacyImportRun.importer_version).limit(1)) == "assets-v1-importer/3.0"
        unresolved = session.scalar(select(LegacyImportRecord).where(LegacyImportRecord.legacy_table == "integration_unresolved"))
        assert unresolved.evidence_json["external_id"] == "unknown-1"


def test_batched_import_commits_deterministic_small_units_and_reruns_safely(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session:
        first = import_v1_assets(session, v1_db, batch_size=1)
    with factory() as session:
        second = import_v1_assets(session, v1_db, batch_size=1)
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
    assert first["counts"]["assets.created"] == 1
    assert second["counts"]["assets.reused"] == 3


def test_invalid_legacy_country_is_reviewable_in_real_import(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db, first_asset_country="Portugal")
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        manifest = import_v1_assets(session, v1_db)
    assert manifest["counts"]["assets.created"] == 1
    assert any(issue["legacy_id"] == "1" and "country value requires review" in issue["reason"] for issue in manifest["issues"])
    with factory() as session:
        asset = session.scalar(select(Asset).where(Asset.canonical_name == "Alpha Solar"))
        assert asset is not None
        assert asset.country_code is None
        assert asset.review_status == "needs_review"
        assert "country value requires review" in asset.review_note


def test_invalid_legacy_country_dry_run_matches_review_classification(tmp_path: Path) -> None:
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db, first_asset_country="Portugal")
    manifest = import_v1_assets(None, v1_db, dry_run=True)
    assert manifest["dry_run"] is True
    assert manifest["counts"]["assets.created"] == 1
    assert any(issue["legacy_id"] == "1" and "country value requires review" in issue["reason"] for issue in manifest["issues"])
    connection = open_v1_readonly(v1_db)
    assert connection.execute("SELECT country FROM assets WHERE id = 1").fetchone()[0] == "Portugal"
    connection.close()


def test_v1_devices_import_with_provenance_and_reviewable_outcomes(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db, with_devices=True)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        manifest = import_v1_assets(session, v1_db)

    # Every source row is accounted for: one imported, one unknown type held for
    # review, one whose parent asset was quarantined by the identity import.
    assert manifest["counts"]["provider_devices.created"] == 1
    assert manifest["counts"]["provider_devices.quarantined"] == 1
    assert manifest["counts"]["provider_devices.excluded"] == 1

    with factory() as session:
        device = session.scalar(select(Device))
        assert device is not None
        assert device.device_kind == "inverter"
        assert device.serial_number == "6T2159042269"
        assert device.normalized_serial_number == "6T2159042269"
        assert device.model == "SUN2000-30KTL-M3"
        assert device.rated_power_kw == Decimal("30.000")
        assert device.lifecycle_status == "active"
        assert device.label == "Inverter 1"

        plant = session.scalar(select(AssetProviderMapping).where(AssetProviderMapping.resource_kind == "plant"))
        claim = session.scalar(select(AssetProviderMapping).where(AssetProviderMapping.resource_kind == "device"))
        assert claim.device_id == device.id
        assert claim.asset_id == device.asset_id
        assert claim.external_id == "1000000139150452"
        assert claim.mapping_status == "pending_review"
        assert claim.parent_mapping_id == plant.id

        record = session.scalar(
            select(LegacyImportRecord).where(
                LegacyImportRecord.legacy_table == "provider_devices",
                LegacyImportRecord.outcome == "created",
            )
        )
        assert record.target_device_id == device.id
        assert record.evidence_json["dev_dn"] == "NE=139150452"
        assert record.evidence_json["station_code"] == "Plant-A"

    # A rerun neither duplicates devices nor re-creates their claims. Every
    # source row replays as reused, including the ones held for review, so the
    # rerun still accounts for all three.
    with factory() as session, session.begin():
        rerun = import_v1_assets(session, v1_db)
    assert rerun["counts"]["provider_devices.reused"] == 3
    assert "provider_devices.created" not in rerun["counts"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Device)) == 1
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 2


def test_import_without_provider_devices_table_still_runs(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        manifest = import_v1_assets(session, v1_db)
    assert manifest["counts"]["assets.created"] == 1
    assert not any(key.startswith("provider_devices.") for key in manifest["counts"])
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Device)) == 0


def test_interrupted_batched_import_resumes_without_duplicates(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_v1_fixture(v1_db)
    source = sqlite3.connect(v1_db)
    source.execute("INSERT INTO assets VALUES (4, 'Bravo Solar', NULL, 'Porto', '5', NULL, 'PT', NULL, NULL, NULL)")
    source.commit()
    source.close()
    factory = build_session_factory(create_engine(settings.database_url))
    original_create_asset = v1_import_module.create_asset

    def fail_on_remaining_asset(*args, **kwargs):
        if kwargs.get("canonical_name") == "Bravo Solar":
            raise RuntimeError("simulated interrupted import")
        return original_create_asset(*args, **kwargs)

    monkeypatch.setattr(v1_import_module, "create_asset", fail_on_remaining_asset)
    with factory() as session:
        with pytest.raises(RuntimeError, match="simulated interrupted import"):
            import_v1_assets(session, v1_db, batch_size=1)

    with factory() as session:
        interrupted = session.scalar(select(LegacyImportRun).order_by(LegacyImportRun.id))
        assert interrupted is not None
        assert interrupted.finished_at is None
        assert session.scalar(select(func.count()).select_from(Asset)) == 1

    monkeypatch.setattr(v1_import_module, "create_asset", original_create_asset)
    with factory() as session:
        resumed = import_v1_assets(session, v1_db, batch_size=1)
    assert resumed["counts"]["assets.created"] == 1
    with factory() as session:
        runs = session.scalars(select(LegacyImportRun).order_by(LegacyImportRun.id)).all()
        assert len(runs) == 2
        assert runs[0].finished_at is None
        assert runs[1].finished_at is not None
        assert session.scalar(select(func.count()).select_from(Asset)) == 2
        assert session.scalar(select(func.count()).select_from(AssetAlias)) == 1
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 1
        assert session.scalar(select(func.count()).select_from(LegacyImportRecord)) > 0
