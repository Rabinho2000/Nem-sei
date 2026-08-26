"""O&M engagements: the derived state, the temporal writes, and the V1 import.

The V1 fixture here is built by hand rather than read from the live database,
so these prove the importer's own logic independently of whether real V1
evidence is reachable. `test_service_contracts_golden.py` is what compares
against the real thing.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from nemsei.assets.service import create_asset
from nemsei.contracts.models import AssetServiceContract
from nemsei.contracts.service import (
    assets_in_om_scope,
    assets_with_active_om,
    close_service_contract,
    contracts_for,
    expiry_bucket,
    om_status,
    overview,
    scoped_asset_ids,
    set_service_contract,
)
from nemsei.contracts.v1_import import import_v1_service_contracts
from nemsei.db.session import build_session_factory
from nemsei.providers.models import LegacyImportRecord, LegacyImportRun
from nemsei.shared.clock import utc_now

TODAY = date(2026, 8, 25)


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def session_factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def build_v1_fixture(path: Path, assets: list[dict], om_contracts: list[dict] | None = None) -> None:
    connection = sqlite3.connect(path)
    try:
        for table in ("customers", "asset_aliases", "asset_integrations"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE assets (id INTEGER PRIMARY KEY, project_name TEXT, maintenance TEXT,"
            " active_contract TEXT, start_contract TEXT, end_contract TEXT, duration TEXT)"
        )
        connection.execute(
            "CREATE TABLE om_contracts (id INTEGER PRIMARY KEY, asset_id INTEGER, contract_start_date TEXT,"
            " contract_end_date TEXT, annual_value REAL, notes TEXT, pdf_path TEXT, original_filename TEXT,"
            " renewal_status TEXT, last_contact_date TEXT, renewal_notes TEXT)"
        )
        for row in assets:
            connection.execute(
                "INSERT INTO assets (id, project_name, maintenance, active_contract, start_contract,"
                " end_contract, duration) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row.get("project_name", f"Central {row['id']}"), row.get("maintenance", "no"),
                    row.get("active_contract", "no"), row.get("start_contract", ""),
                    row.get("end_contract", ""), row.get("duration", ""),
                ),
            )
        for index, row in enumerate(om_contracts or [], start=1):
            connection.execute(
                "INSERT INTO om_contracts (id, asset_id, contract_start_date, contract_end_date, annual_value,"
                " notes, pdf_path, original_filename, renewal_status, last_contact_date, renewal_notes)"
                " VALUES (?, ?, ?, ?, ?, ?, '', '', NULL, NULL, NULL)",
                (
                    index, row["asset_id"], row.get("contract_start_date", ""), row.get("contract_end_date", ""),
                    row.get("annual_value"), row.get("notes"),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def link_legacy(session, *, legacy_id: int, asset_id: int, run: LegacyImportRun) -> None:
    session.add(
        LegacyImportRecord(
            import_run_id=run.id, source_database_sha256="0" * 64, source_locator_sha256="0" * 64,
            legacy_table="assets", legacy_id=str(legacy_id), source_hash=f"{legacy_id:064d}",
            outcome="created", evidence_json={}, target_asset_id=asset_id, created_at=utc_now(),
        )
    )


def make_run(session) -> LegacyImportRun:
    run = LegacyImportRun(
        source_database_sha256="0" * 64, source_locator_sha256="0" * 64, dry_run=False,
        started_at=utc_now(), manifest_json={}, importer_version="test",
    )
    session.add(run)
    session.flush()
    return run


# --- derived state ---------------------------------------------------------


def test_status_is_derived_from_the_period_not_stored(session_factory):
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Colmeia")
        session.flush()
        set_service_contract(
            session, asset_id=plant.id, created_by="tester",
            valid_from=date(2019, 9, 16), valid_to=date(2026, 9, 17),
        )
        session.flush()
        assert om_status(session, asset_id=plant.id, on=TODAY) == "active"
        # `valid_to` is exclusive: the last covered day is the day before it.
        assert om_status(session, asset_id=plant.id, on=date(2026, 9, 16)) == "active"
        assert om_status(session, asset_id=plant.id, on=date(2026, 9, 17)) == "expired"
        # Nothing was written to make that change true.
        assert om_status(session, asset_id=plant.id, on=TODAY) == "active"


def test_an_undated_engagement_is_neither_active_nor_expired(session_factory):
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Jetesetecar")
        session.flush()
        set_service_contract(
            session, asset_id=plant.id, created_by="tester", valid_from=None, valid_to=None,
            review_status="needs_review", review_note="sem datas no V1",
        )
        session.flush()
        assert om_status(session, asset_id=plant.id, on=TODAY) == "undated"
        # In scope, and still alerted: the plant is operated, the paperwork is
        # what is missing.
        assert plant.id in assets_in_om_scope(session, on=TODAY)
        assert plant.id in assets_with_active_om(session, on=TODAY)


def test_an_asset_without_any_engagement_is_out_of_scope(session_factory):
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Sem O&M")
        session.flush()
        assert om_status(session, asset_id=plant.id, on=TODAY) == "none"
        assert assets_in_om_scope(session, on=TODAY) == set()


def test_a_lapsed_engagement_stays_in_scope_but_leaves_the_active_set(session_factory):
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Motassis")
        session.flush()
        set_service_contract(
            session, asset_id=plant.id, created_by="tester",
            valid_from=date(2024, 5, 1), valid_to=date(2025, 5, 2),
        )
        session.flush()
        assert om_status(session, asset_id=plant.id, on=TODAY) == "expired"
        assert plant.id in assets_in_om_scope(session, on=TODAY)
        assert plant.id not in assets_with_active_om(session, on=TODAY)


def test_expiry_buckets_follow_the_renewal_horizon():
    assert expiry_bucket(TODAY - timedelta(days=1), on=TODAY) == "expired"
    assert expiry_bucket(TODAY + timedelta(days=30), on=TODAY) == "within_90_days"
    assert expiry_bucket(TODAY + timedelta(days=200), on=TODAY) == "within_a_year"
    assert expiry_bucket(TODAY + timedelta(days=800), on=TODAY) == "beyond_a_year"
    assert expiry_bucket(None, on=TODAY) == "undated"


# --- temporal writes -------------------------------------------------------


def test_a_renewal_is_a_new_row_that_closes_the_old_one(session_factory):
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Florineve")
        session.flush()
        set_service_contract(
            session, asset_id=plant.id, created_by="tester",
            valid_from=date(2019, 12, 4), valid_to=date(2026, 12, 5), annual_value_eur=Decimal("2000.00"),
        )
        session.flush()
        set_service_contract(
            session, asset_id=plant.id, created_by="tester",
            valid_from=date(2026, 9, 1), annual_value_eur=Decimal("2650.00"),
        )
        session.commit()
        rows = contracts_for(session, asset_id=plant.id)
        assert len(rows) == 2
        # The terms that were true last year survive the renewal, which V1's
        # UNIQUE (asset_id) made impossible.
        assert {row.annual_value_eur for row in rows} == {Decimal("2000.00"), Decimal("2650.00")}
        closed = next(row for row in rows if row.valid_from == date(2019, 12, 4))
        assert closed.valid_to == date(2026, 9, 1)


def test_two_engagements_may_not_claim_the_same_day(session_factory):
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Sicobrita")
        session.flush()
        set_service_contract(
            session, asset_id=plant.id, created_by="tester",
            valid_from=date(2026, 1, 1), valid_to=date(2027, 1, 1),
        )
        session.flush()
        with pytest.raises(ValueError, match="começa em"):
            set_service_contract(
                session, asset_id=plant.id, created_by="tester", valid_from=date(2025, 6, 1),
            )


def test_the_database_refuses_an_overlap_the_service_never_produces(session_factory):
    """The exclusion constraint is the backstop, not the service's politeness."""
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Roufimar")
        session.flush()
        set_service_contract(
            session, asset_id=plant.id, created_by="tester",
            valid_from=date(2026, 1, 1), valid_to=date(2027, 1, 1),
        )
        session.commit()
        with pytest.raises(Exception) as caught:
            session.execute(
                text(
                    "INSERT INTO asset_service_contracts (public_id, asset_id, service_kind, valid_from,"
                    " valid_to, source_kind, provenance_json, review_status, created_by, created_at, updated_at)"
                    " VALUES ('dup', :asset_id, 'om', '2026-06-01', '2028-01-01', 'operator', '{}', 'clear',"
                    " 'tester', now(), now())"
                ),
                {"asset_id": plant.id},
            )
        assert "ex_asset_service_contracts_no_overlap" in str(caught.value)
        session.rollback()


def test_closing_an_engagement_needs_a_date_after_its_start(session_factory):
    with session_factory() as session:
        plant = create_asset(session, canonical_name="Viarco")
        session.flush()
        contract = set_service_contract(
            session, asset_id=plant.id, created_by="tester", valid_from=date(2026, 1, 1),
        )
        session.flush()
        with pytest.raises(ValueError):
            close_service_contract(session, contract_id=contract.id, valid_to=date(2025, 1, 1), actor="tester")
        close_service_contract(session, contract_id=contract.id, valid_to=date(2026, 7, 1), actor="tester")
        assert om_status(session, asset_id=plant.id, on=TODAY) == "expired"


def test_scopes_distinguish_everything_from_nothing(session_factory):
    with session_factory() as session:
        operated = create_asset(session, canonical_name="Operada")
        lapsed = create_asset(session, canonical_name="Caducada")
        create_asset(session, canonical_name="Fora de âmbito")
        session.flush()
        set_service_contract(
            session, asset_id=operated.id, created_by="tester", valid_from=date(2025, 1, 1),
        )
        set_service_contract(
            session, asset_id=lapsed.id, created_by="tester",
            valid_from=date(2023, 1, 1), valid_to=date(2024, 1, 1),
        )
        session.flush()
        # "all" is None, never the set of everything: a policy that matches
        # nothing and one that matches the fleet must not look alike.
        assert scoped_asset_ids(session, asset_scope="all", on=TODAY) is None
        assert scoped_asset_ids(session, asset_scope="om", on=TODAY) == {operated.id, lapsed.id}
        assert scoped_asset_ids(session, asset_scope="om_active", on=TODAY) == {operated.id}


def test_overview_counts_what_the_panel_shows(session_factory):
    with session_factory() as session:
        active = create_asset(session, canonical_name="Ativa")
        lapsed = create_asset(session, canonical_name="Caducada")
        undated = create_asset(session, canonical_name="Sem datas")
        create_asset(session, canonical_name="Fora")
        session.flush()
        set_service_contract(session, asset_id=active.id, created_by="t", valid_from=date(2025, 1, 1), valid_to=date(2030, 1, 1))
        set_service_contract(session, asset_id=lapsed.id, created_by="t", valid_from=date(2023, 1, 1), valid_to=date(2024, 1, 1))
        set_service_contract(session, asset_id=undated.id, created_by="t", valid_from=None, valid_to=None)
        session.flush()
        summary = overview(session, on=TODAY)
        assert summary["in_scope"] == 3
        assert summary["active"] == 1
        assert summary["expired"] == 1
        assert summary["undated"] == 1
        assert summary["total_assets"] == 4


# --- the V1 import ---------------------------------------------------------


def test_import_carries_the_period_and_corrects_the_inclusive_end(session_factory, tmp_path):
    source = tmp_path / "v1.db"
    build_v1_fixture(
        source,
        assets=[
            {"id": 1849, "maintenance": "yes", "active_contract": "yes",
             "start_contract": "2019-09-16", "end_contract": "2026-09-16", "duration": "7"},
            {"id": 9999, "maintenance": "no", "active_contract": "no"},
        ],
    )
    with session_factory() as session:
        run = make_run(session)
        plant = create_asset(session, canonical_name="Colmeia")
        session.flush()
        link_legacy(session, legacy_id=1849, asset_id=plant.id, run=run)
        session.flush()

        manifest = import_v1_service_contracts(
            session, source, operator="tester", dry_run=False, as_of=TODAY
        )
        session.flush()

        assert manifest["counts"]["contracts_created"] == 1
        assert manifest["counts"]["assets_out_of_scope"] == 1
        contract = contracts_for(session, asset_id=plant.id)[0]
        assert contract.valid_from == date(2019, 9, 16)
        # V1's end date is the last day covered; V2's bound is exclusive.
        assert contract.valid_to == date(2026, 9, 17)
        assert contract.provenance_json["v1_end_inclusive"] == "2026-09-16"
        assert contract.source_kind == "v1_import"
        assert om_status(session, asset_id=plant.id, on=TODAY) == "active"


def test_import_verifies_v1s_derived_flag_instead_of_copying_it(session_factory, tmp_path):
    source = tmp_path / "v1.db"
    build_v1_fixture(
        source,
        assets=[
            # Ended last year: V1 stores "no" and the derivation agrees.
            {"id": 1953, "maintenance": "yes", "active_contract": "no",
             "start_contract": "2024-05-01", "end_contract": "2025-05-01"},
            # V1 stores "yes" on a contract that ended: a drifted flag, which
            # is reported and never written.
            {"id": 1954, "maintenance": "yes", "active_contract": "yes",
             "start_contract": "2024-09-26", "end_contract": "2025-09-26"},
        ],
    )
    with session_factory() as session:
        run = make_run(session)
        lapsed = create_asset(session, canonical_name="Motassis")
        drifted = create_asset(session, canonical_name="Lusitana")
        session.flush()
        link_legacy(session, legacy_id=1953, asset_id=lapsed.id, run=run)
        link_legacy(session, legacy_id=1954, asset_id=drifted.id, run=run)
        session.flush()

        manifest = import_v1_service_contracts(
            session, source, operator="tester", dry_run=False, as_of=TODAY
        )
        session.flush()

        assert manifest["counts"]["derivation_checked"] == 2
        assert manifest["counts"]["derivation_mismatches"] == 1
        mismatch = [issue for issue in manifest["issues"] if issue["reason"] == "active_contract_mismatch"]
        assert len(mismatch) == 1 and mismatch[0]["legacy_id"] == "1954"
        # Both are expired on the evidence, whatever V1's flag said.
        assert om_status(session, asset_id=drifted.id, on=TODAY) == "expired"


def test_import_prefers_the_contracts_table_over_the_asset_columns(session_factory, tmp_path):
    source = tmp_path / "v1.db"
    build_v1_fixture(
        source,
        assets=[{"id": 2115, "maintenance": "yes", "active_contract": "yes", "end_contract": "2030-01-01"}],
        om_contracts=[{"asset_id": 2115, "contract_end_date": "2037-01-07", "annual_value": 2650.0, "notes": "1 limpeza feita"}],
    )
    with session_factory() as session:
        run = make_run(session)
        plant = create_asset(session, canonical_name="Sicobrita")
        session.flush()
        link_legacy(session, legacy_id=2115, asset_id=plant.id, run=run)
        session.flush()
        import_v1_service_contracts(session, source, operator="tester", dry_run=False, as_of=TODAY)
        session.flush()
        contract = contracts_for(session, asset_id=plant.id)[0]
        assert contract.valid_to == date(2037, 1, 8)
        assert contract.annual_value_eur == Decimal("2650.00")
        assert contract.notes == "1 limpeza feita"
        # No start date anywhere in V1, so the row says so rather than guessing.
        assert contract.valid_from is None
        assert contract.review_status == "needs_review"


def test_import_flags_a_one_day_window_but_keeps_what_v1_stated(session_factory, tmp_path):
    source = tmp_path / "v1.db"
    build_v1_fixture(
        source,
        assets=[{"id": 1900, "maintenance": "yes", "active_contract": "no",
                 "start_contract": "2023-03-01", "end_contract": "2023-03-01"}],
    )
    with session_factory() as session:
        run = make_run(session)
        plant = create_asset(session, canonical_name="Vatel")
        session.flush()
        link_legacy(session, legacy_id=1900, asset_id=plant.id, run=run)
        session.flush()
        manifest = import_v1_service_contracts(session, source, operator="tester", dry_run=False, as_of=TODAY)
        session.flush()
        assert [issue["reason"] for issue in manifest["issues"]].count("one_day_window") == 1
        contract = contracts_for(session, asset_id=plant.id)[0]
        # V1's end date is inclusive, so start == end is a one-day contract.
        # Implausible for O&M, so it is flagged -- but not rewritten.
        assert contract.valid_from == date(2023, 3, 1)
        assert contract.valid_to == date(2023, 3, 2)
        assert contract.review_status == "needs_review"
        assert "um dia" in contract.review_note
        assert contract.provenance_json["v1_end_inclusive"] == "2023-03-01"


def test_import_is_idempotent(session_factory, tmp_path):
    source = tmp_path / "v1.db"
    build_v1_fixture(
        source,
        assets=[{"id": 1849, "maintenance": "yes", "active_contract": "yes",
                 "start_contract": "2019-09-16", "end_contract": "2026-09-16"}],
    )
    with session_factory() as session:
        run = make_run(session)
        plant = create_asset(session, canonical_name="Colmeia")
        session.flush()
        link_legacy(session, legacy_id=1849, asset_id=plant.id, run=run)
        session.flush()
        import_v1_service_contracts(session, source, operator="tester", dry_run=False, as_of=TODAY)
        session.commit()
        second = import_v1_service_contracts(session, source, operator="tester", dry_run=False, as_of=TODAY)
        session.commit()
        assert second["counts"]["contracts_created"] == 0
        assert second["counts"]["contracts_existing"] == 1
        assert session.query(AssetServiceContract).count() == 1


def test_dry_run_writes_nothing(session_factory, tmp_path):
    source = tmp_path / "v1.db"
    build_v1_fixture(
        source,
        assets=[{"id": 1849, "maintenance": "yes", "active_contract": "yes",
                 "start_contract": "2019-09-16", "end_contract": "2026-09-16"}],
    )
    with session_factory() as session:
        run = make_run(session)
        plant = create_asset(session, canonical_name="Colmeia")
        session.flush()
        link_legacy(session, legacy_id=1849, asset_id=plant.id, run=run)
        session.flush()
        manifest = import_v1_service_contracts(session, source, operator="tester", dry_run=True, as_of=TODAY)
        assert manifest["counts"]["contracts_created"] == 1
        assert session.query(AssetServiceContract).count() == 0


def test_import_counts_a_scoped_asset_that_never_reached_v2(session_factory, tmp_path):
    source = tmp_path / "v1.db"
    build_v1_fixture(
        source,
        assets=[{"id": 4242, "project_name": "Nunca importada", "maintenance": "yes", "active_contract": "yes",
                 "end_contract": "2030-01-01"}],
    )
    with session_factory() as session:
        make_run(session)
        session.flush()
        manifest = import_v1_service_contracts(session, source, operator="tester", dry_run=False, as_of=TODAY)
        assert manifest["counts"]["assets_without_v2_match"] == 1
        assert manifest["issues"][0]["reason"] == "asset_not_imported"
        assert session.query(AssetServiceContract).count() == 0
