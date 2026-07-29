from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from app import ensure_database
from monitoring_board.asset_capacity_repository import (
    add_capacity_expansion,
    add_capacity_period,
    ensure_asset_capacity_schema,
    resolve_capacity_at,
    resolve_capacity_for_period,
)
from monitoring_board.portfolio_reports import build_portfolio_report_rows


def connect(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "capacity.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_database(str(tmp_path / "capacity.db"))
    return conn


def add_asset(conn, *, kwp: str = "10", mounting_date: str | None = None) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO assets (project_name, kwp, mounting_date)
            VALUES ('Central Sintética', ?, ?)
            """,
            (kwp, mounting_date),
        ).lastrowid
    )


def test_capacity_falls_back_to_asset_kwp_without_history(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn, kwp="12.5")

    result = resolve_capacity_at(
        conn,
        asset_id=asset_id,
        target_date=date(2026, 1, 1),
        fallback_kwp="12.5",
    )

    assert result.installed_power_kwp == Decimal("12.5")
    assert result.source == "asset_kwp"


def test_capacity_selects_period_and_expansion_closes_previous(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    first_id = add_capacity_period(
        conn,
        asset_id=asset_id,
        valid_from="2025-01-01",
        valid_to=None,
        installed_power_kwp="10",
    )
    second_id = add_capacity_expansion(
        conn,
        asset_id=asset_id,
        valid_from="2026-07-01",
        installed_power_kwp="15",
        reason="Expansão",
    )

    before = resolve_capacity_at(
        conn, asset_id=asset_id, target_date="2026-06-30", fallback_kwp="10"
    )
    after = resolve_capacity_at(
        conn, asset_id=asset_id, target_date="2026-07-01", fallback_kwp="10"
    )
    first = conn.execute(
        "SELECT valid_to FROM asset_capacity_periods WHERE id = ?", (first_id,)
    ).fetchone()
    assert before.installed_power_kwp == Decimal("10")
    assert after.installed_power_kwp == Decimal("15")
    assert after.period_id == second_id
    assert first["valid_to"] == "2026-06-30"


def test_capacity_rejects_overlap_and_ambiguous_report_month(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    add_capacity_period(
        conn,
        asset_id=asset_id,
        valid_from="2026-01-01",
        valid_to="2026-06-30",
        installed_power_kwp="10",
    )
    with pytest.raises(ValueError, match="capacity_period_overlap"):
        add_capacity_period(
            conn,
            asset_id=asset_id,
            valid_from="2026-06-01",
            valid_to=None,
            installed_power_kwp="15",
        )
    add_capacity_period(
        conn,
        asset_id=asset_id,
        valid_from="2026-07-01",
        valid_to=None,
        installed_power_kwp="15",
    )

    resolution = resolve_capacity_for_period(
        conn,
        asset_id=asset_id,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 31),
        fallback_kwp="10",
    )
    assert resolution.ambiguous is True
    assert resolution.installed_power_kwp is None


def test_initial_backfill_only_uses_safe_known_date_and_is_idempotent(tmp_path) -> None:
    conn = connect(tmp_path)
    dated = add_asset(conn, kwp="20", mounting_date="2024-05-01")
    undated = add_asset(conn, kwp="30")

    ensure_asset_capacity_schema(conn)
    ensure_asset_capacity_schema(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM asset_capacity_periods WHERE asset_id = ?", (dated,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_capacity_periods WHERE asset_id = ?", (undated,)
    ).fetchone()[0] == 0


def test_portfolio_specific_yield_uses_historical_capacity(tmp_path) -> None:
    conn = connect(tmp_path)
    asset_id = add_asset(conn, kwp="10")
    portfolio_id = int(
        conn.execute(
            "INSERT INTO portfolio_groups (name, notes) VALUES ('Portfolio Capacidade', '')"
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO portfolio_assets (
            portfolio_id, asset_id, active, mapping_status, mapping_confidence
        ) VALUES (?, ?, 1, 'manual', 1)
        """,
        (portfolio_id, asset_id),
    )
    add_capacity_period(
        conn,
        asset_id=asset_id,
        valid_from="2026-01-01",
        valid_to=None,
        installed_power_kwp="20",
    )
    conn.execute(
        """
        INSERT INTO production_records (
            asset_id, provider, period_type, period_date, production_kwh,
            created_at, updated_at, payload_json
        ) VALUES (?, 'FusionSolar', 'month', '2026-01-01', 200, '2026-02-01', '2026-02-01', '{}')
        """,
        (asset_id,),
    )
    source_id = int(
        conn.execute(
            """
            INSERT INTO source_files (
                asset_id, file_type, original_filename, stored_path, uploaded_at
            ) VALUES (?, 'helioscope', 'expected.xlsx', 'expected.xlsx', '2026-01-01')
            """,
            (asset_id,),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO helioscope_expected_production (
            asset_id, source_file_id, base_year, month, expected_kwh, imported_at
        ) VALUES (?, ?, 2026, 1, 200, '2026-01-01')
        """,
        (asset_id, source_id),
    )

    row = build_portfolio_report_rows(
        conn,
        portfolio_id,
        "2026-01",
        reference_date=date(2026, 2, 1),
    )[0]

    assert row["installed_power_kwp"] == 20
    assert row["installed_power_source"] == "capacity_history"
    assert row["expected_specific_yield"] == 10
