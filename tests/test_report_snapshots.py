from __future__ import annotations

import json
import sqlite3
import pytest

from app import ensure_database
from monitoring_board.reporting.quality_gate import (
    BLOCKED,
    READY,
    evaluate_report_quality,
)
from monitoring_board.reporting.snapshots import (
    approve_snapshot,
    approved_snapshot_for_period,
    canonical_json,
    create_snapshot,
    get_snapshot,
    reject_snapshot,
    snapshot_hash,
    validate_snapshot,
)


def connect(tmp_path) -> sqlite3.Connection:
    path = tmp_path / "snapshots.db"
    ensure_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_asset(conn) -> int:
    return int(
        conn.execute(
            "INSERT INTO assets (project_name, kwp) VALUES ('Central Snapshot', '10')"
        ).lastrowid
    )


def ready_payload(asset_id: int) -> dict:
    return {
        "asset_id": asset_id,
        "installation": "Central Snapshot",
        "energy_provider": "FusionSolar",
        "production_quality_status": "complete",
        "production_source": "monthly",
        "production_kwh": "100.00",
        "availability_pct": "99.0",
        "invoice_status": "confirmed",
    }


def test_missing_financial_model_blocks_only_when_template_depends_on_expected_production(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    payload = {
        **ready_payload(asset_id),
        "expected_production_kwh": None,
        "expected_production_source": "missing",
    }

    optional = evaluate_report_quality(payload, scope="individual")
    required = evaluate_report_quality(
        payload, scope="individual", requires_expected_production=True
    )

    assert optional.status == "warning"
    assert required.status == BLOCKED
    assert required.blockers[0].code == "missing_financial_model_expected_production"


def test_individual_snapshot_hash_is_deterministic_and_freezes_configuration(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    payload = ready_payload(asset_id)
    first = create_snapshot(
        conn,
        scope_type="individual",
        asset_id=asset_id,
        period_type="monthly",
        period_start="2026-01-01",
        period_end="2026-01-31",
        payload=payload,
        template_id=2,
        template_version=4,
        template_snapshot={"title": "Versão quatro"},
        billing_snapshot={"price": "0.1000"},
        energy_sources=[{"provider": "FusionSolar", "record_ids": [1]}],
        source_versions={"production_record_ids": [1]},
        coverage={"days": 31},
        engine_version="report-v2",
        created_by="tester",
    )
    second = create_snapshot(
        conn,
        scope_type="individual",
        asset_id=asset_id,
        period_type="monthly",
        period_start="2026-01-01",
        period_end="2026-01-31",
        payload=dict(reversed(list(payload.items()))),
        template_id=2,
        template_version=4,
        template_snapshot={"title": "Versão quatro"},
        billing_snapshot={"price": "0.1000"},
        energy_sources=[{"record_ids": [1], "provider": "FusionSolar"}],
        source_versions={"production_record_ids": [1]},
        coverage={"days": 31},
        engine_version="report-v2",
    )

    assert get_snapshot(conn, first).data_hash == get_snapshot(conn, second).data_hash
    assert get_snapshot(conn, first).template_version == 4
    row = conn.execute("SELECT * FROM report_snapshots WHERE id = ?", (first,)).fetchone()
    assert json.loads(row["billing_snapshot_json"]) == {"price": "0.1000"}


def test_portfolio_snapshot_approval_is_immutable_and_survives_base_changes(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    portfolio_id = int(
        conn.execute(
            "INSERT INTO portfolio_groups (name, notes) VALUES ('Portfolio Snapshot', '')"
        ).lastrowid
    )
    payload = {"rows": [ready_payload(asset_id)], "summary": {"production_kwh": "100.00"}}
    snapshot_id = create_snapshot(
        conn,
        scope_type="portfolio",
        portfolio_id=portfolio_id,
        period_type="monthly",
        period_start="2026-01-01",
        period_end="2026-01-31",
        payload=payload,
        profile_id=3,
        profile_version=7,
        profile_snapshot={"columns": ["production_kwh"]},
        template_id=4,
        template_version=2,
        template_snapshot={"sections": ["kpis"]},
        engine_version="portfolio-v2",
    )
    quality = evaluate_report_quality(payload, scope="portfolio")
    assert quality.status == READY
    assert validate_snapshot(conn, snapshot_id, quality, actor="validator") == "validated"
    approve_snapshot(conn, snapshot_id, actor="approver")
    conn.execute("UPDATE assets SET kwp = '999' WHERE id = ?", (asset_id,))

    frozen = get_snapshot(conn, snapshot_id)
    assert frozen.payload["summary"]["production_kwh"] == "100.00"
    assert frozen.profile_version == 7
    assert frozen.template_version == 2
    with pytest.raises(sqlite3.IntegrityError, match="approved_snapshot_immutable"):
        conn.execute(
            "UPDATE report_snapshots SET payload_json = '{}' WHERE id = ?",
            (snapshot_id,),
        )


def test_blocked_or_rejected_snapshot_cannot_be_selected_as_approved(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    payload = {
        **ready_payload(asset_id),
        "production_quality_status": "partial",
    }
    snapshot_id = create_snapshot(
        conn,
        scope_type="individual",
        asset_id=asset_id,
        period_type="monthly",
        period_start="2026-01-01",
        period_end="2026-01-31",
        payload=payload,
        engine_version="report-v2",
    )
    quality = evaluate_report_quality(payload, scope="individual")
    assert quality.status == BLOCKED
    assert validate_snapshot(conn, snapshot_id, quality) == "blocked"
    with pytest.raises(ValueError, match="snapshot_approval_blocked"):
        approve_snapshot(conn, snapshot_id, actor="approver")
    reject_snapshot(conn, snapshot_id, actor="reviewer", reason="Produção parcial")
    assert get_snapshot(conn, snapshot_id).approval_status == "rejected"
    assert approved_snapshot_for_period(
        conn,
        scope_type="individual",
        asset_id=asset_id,
        period_start="2026-01-01",
    ) is None


def test_hash_detects_tampering_before_validation(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    payload = ready_payload(asset_id)
    snapshot_id = create_snapshot(
        conn,
        scope_type="individual",
        asset_id=asset_id,
        period_type="monthly",
        period_start="2026-01-01",
        period_end="2026-01-31",
        payload=payload,
        engine_version="report-v2",
    )
    conn.execute(
        "UPDATE report_snapshots SET payload_json = ? WHERE id = ?",
        (canonical_json({**payload, "production_kwh": "101.00"}), snapshot_id),
    )
    with pytest.raises(ValueError, match="snapshot_hash_invalid"):
        validate_snapshot(
            conn,
            snapshot_id,
            evaluate_report_quality(payload, scope="individual"),
        )


def test_snapshot_hash_helper_is_order_independent() -> None:
    assert snapshot_hash({"b": 2, "a": 1}) == snapshot_hash({"a": 1, "b": 2})
