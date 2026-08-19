from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select

from nemsei.assets.identity_decisions import (
    decision_supersedes_prior_record,
    load_identity_decisions,
    record_identity_decision,
    resolve_duplicate_groups,
)
from nemsei.assets.models import Asset, AssetAlias, Device
from nemsei.assets.v1_import import import_v1_assets
from nemsei.db.session import build_session_factory
from nemsei.providers.models import AssetProviderMapping, LegacyImportRecord, OperatorAuditEvent


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def build_duplicate_fixture(path: Path) -> None:
    """Mirror the real V1 shape: one populated row and one empty twin."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, nif TEXT, normalized_nif TEXT, active INTEGER, review_required INTEGER, review_notes TEXT);
            CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT NOT NULL, address TEXT, location TEXT, kwp TEXT, commissioning_date TEXT, country TEXT, timezone TEXT, notes TEXT, customer_id INTEGER);
            CREATE TABLE asset_aliases (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, alias_name TEXT NOT NULL, normalized_alias TEXT NOT NULL, source TEXT, active INTEGER);
            CREATE TABLE asset_integrations (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, provider TEXT NOT NULL, external_id TEXT, external_name TEXT, enabled INTEGER);
            CREATE TABLE provider_devices (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, provider TEXT NOT NULL, station_code TEXT, external_device_id TEXT, dev_dn TEXT, sn TEXT, device_name TEXT, model TEXT, rated_power_kw REAL, dev_type_id INTEGER, enabled INTEGER);
            INSERT INTO customers VALUES (1, 'Owner One', 'PT501936270', '501936270', 1, 0, NULL);
            INSERT INTO assets VALUES (10, 'Twinned Plant', 'Road 1', 'Evora', '23.6', NULL, 'PT', NULL, NULL, 1);
            INSERT INTO assets VALUES (20, 'Twinned Plant', 'Road 1', 'Evora', '23.6', NULL, 'PT', NULL, NULL, 1);
            INSERT INTO asset_aliases VALUES (1, 10, 'Twinned Plant', 'twinned plant', 'excel', 1);
            INSERT INTO asset_integrations VALUES (1, 10, 'FusionSolar', 'NE=191807662', 'Twinned Plant', 1);
            INSERT INTO provider_devices VALUES (1, 10, 'FusionSolar', 'NE=191807662', '1000000191807670', 'NE=191807670', 'NS2441415182', 'INV-1', 'SUN2000-20K-MB0', 20.0, 1, 1);
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_a_group_resolves_only_when_every_member_is_decided() -> None:
    groups = {"twinned plant": [10, 20]}
    assert resolve_duplicate_groups(groups, {}) == {}
    assert resolve_duplicate_groups(groups, {"10": "canonical"}) == {}
    assert resolve_duplicate_groups(groups, {"10": "canonical", "20": "canonical"}) == {}
    assert resolve_duplicate_groups(groups, {"10": "discard", "20": "discard"}) == {}
    assert resolve_duplicate_groups(groups, {"10": "canonical", "20": "discard"}) == {"twinned plant": 10}


def test_replay_is_only_superseded_by_an_actual_decision() -> None:
    # No decision: an undecided quarantine must keep replaying untouched.
    assert not decision_supersedes_prior_record("quarantined", False, group_resolved=False, row_is_canonical=False)
    # Decided: the canonical row reopens, the discarded row records the decision once.
    assert decision_supersedes_prior_record("quarantined", False, group_resolved=True, row_is_canonical=True)
    assert decision_supersedes_prior_record("quarantined", False, group_resolved=True, row_is_canonical=False)
    assert not decision_supersedes_prior_record("excluded", False, group_resolved=True, row_is_canonical=False)
    # Anything that already produced a V2 row is never reopened.
    assert not decision_supersedes_prior_record("created", True, group_resolved=True, row_is_canonical=True)
    assert not decision_supersedes_prior_record(None, False, group_resolved=True, row_is_canonical=True)


def test_decisions_are_recorded_once_and_audited(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        record_identity_decision(session, legacy_table="assets", legacy_id=10, decision="canonical", actor_username="operator", reason="Holds all operational history.")
        record_identity_decision(session, legacy_table="assets", legacy_id=20, decision="discard", actor_username="operator")
        # A revised ruling updates the existing row rather than adding a second.
        record_identity_decision(session, legacy_table="assets", legacy_id=20, decision="discard", actor_username="operator", reason="Empty duplicate.")
    with factory() as session:
        assert load_identity_decisions(session, legacy_table="assets") == {"10": "canonical", "20": "discard"}
        assert load_identity_decisions(session, legacy_table="customers") == {}
        events = session.scalars(select(OperatorAuditEvent).where(OperatorAuditEvent.action == "identity_decision_recorded")).all()
        assert len(events) == 3
        assert events[0].metadata_json == {"legacy_table": "assets", "legacy_id": "10", "decision": "canonical"}


def test_invalid_decisions_are_rejected(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        with pytest.raises(ValueError, match="canonical or discard"):
            record_identity_decision(session, legacy_table="assets", legacy_id=10, decision="merge", actor_username="operator")
        with pytest.raises(ValueError, match="legacy table"):
            record_identity_decision(session, legacy_table="assets", legacy_id=10, decision="canonical", actor_username="  ")


def test_undecided_duplicates_stay_quarantined(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_duplicate_fixture(v1_db)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        manifest = import_v1_assets(session, v1_db)
    assert manifest["counts"]["assets.quarantined"] == 2
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 0
        assert session.scalar(select(func.count()).select_from(Device)) == 0


def test_a_decided_duplicate_imports_the_canonical_row_and_its_children(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_duplicate_fixture(v1_db)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        record_identity_decision(session, legacy_table="assets", legacy_id=10, decision="canonical", actor_username="operator", reason="Holds all operational history.")
        record_identity_decision(session, legacy_table="assets", legacy_id=20, decision="discard", actor_username="operator", reason="Empty duplicate row.")
    with factory() as session, session.begin():
        manifest = import_v1_assets(session, v1_db)

    assert manifest["counts"]["assets.created"] == 1
    assert manifest["counts"]["assets.excluded"] == 1
    assert "assets.quarantined" not in manifest["counts"]
    # The children the quarantine was blocking come through with the canonical row.
    assert manifest["counts"]["asset_aliases.created"] == 1
    assert manifest["counts"]["asset_integrations.created"] == 1
    assert manifest["counts"]["provider_devices.created"] == 1

    with factory() as session:
        asset = session.scalar(select(Asset))
        assert asset is not None and asset.canonical_name == "Twinned Plant"
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(AssetAlias)) == 1
        assert session.scalar(select(func.count()).select_from(Device)) == 1
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 2
        discarded = session.scalar(
            select(LegacyImportRecord).where(
                LegacyImportRecord.legacy_table == "assets",
                LegacyImportRecord.outcome == "excluded",
            )
        )
        assert discarded.legacy_id == "20"
        assert "in favour of V1 asset 10" in discarded.reason


def test_a_decision_reopens_an_already_quarantined_row(settings, monkeypatch, tmp_path: Path) -> None:
    """The real M3 case: quarantine first, decide afterwards, same source."""
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_duplicate_fixture(v1_db)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        first = import_v1_assets(session, v1_db)
    assert first["counts"]["assets.quarantined"] == 2

    with factory() as session, session.begin():
        record_identity_decision(session, legacy_table="assets", legacy_id=10, decision="canonical", actor_username="operator")
        record_identity_decision(session, legacy_table="assets", legacy_id=20, decision="discard", actor_username="operator")
    with factory() as session, session.begin():
        second = import_v1_assets(session, v1_db)

    assert second["counts"]["assets.created"] == 1
    assert second["counts"]["assets.excluded"] == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(Device)) == 1
        # The earlier quarantine stays on the record next to the new outcome.
        outcomes = set(
            session.scalars(
                select(LegacyImportRecord.outcome).where(
                    LegacyImportRecord.legacy_table == "assets",
                    LegacyImportRecord.legacy_id == "10",
                )
            )
        )
        assert outcomes == {"quarantined", "created"}

    # A third run must not re-open anything or duplicate evidence.
    with factory() as session, session.begin():
        third = import_v1_assets(session, v1_db)
    assert third["counts"]["assets.reused"] == 2
    assert "assets.created" not in third["counts"]
    assert "assets.excluded" not in third["counts"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1


def test_a_decided_import_stays_idempotent(settings, monkeypatch, tmp_path: Path) -> None:
    upgrade(settings, monkeypatch)
    v1_db = tmp_path / "v1.db"
    build_duplicate_fixture(v1_db)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        record_identity_decision(session, legacy_table="assets", legacy_id=10, decision="canonical", actor_username="operator")
        record_identity_decision(session, legacy_table="assets", legacy_id=20, decision="discard", actor_username="operator")
    with factory() as session, session.begin():
        import_v1_assets(session, v1_db)
    with factory() as session, session.begin():
        rerun = import_v1_assets(session, v1_db)
    assert rerun["counts"]["assets.reused"] == 2
    assert "assets.created" not in rerun["counts"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(Device)) == 1
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 2
