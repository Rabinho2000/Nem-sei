from __future__ import annotations

import json
from datetime import date

from app import ensure_database, recalculate_performance_references, run_fusionsolar_production_backfill, upsert_production_record
import app as app_module
from monitoring_board.db import get_db


def make_conn(tmp_path):
    db_path = tmp_path / "references.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    conn.execute(
        """
        INSERT INTO integration_configs (
            provider, username, password, base_url, login_endpoint, plants_endpoint,
            real_time_endpoint, alarms_endpoint, day_kpi_endpoint, month_kpi_endpoint,
            enabled, auto_sync_enabled, sync_hours, created_at, updated_at
        ) VALUES ('FusionSolar', 'user', 'secret', 'https://fusion.test', '/login', '/stations',
            '/real', '/alarms', '/day', '/month', 1, 0, '08:00', '2026-01-01', '2026-01-01')
        """
    )
    cursor = conn.execute("INSERT INTO assets (project_name, kwp) VALUES (?, ?)", ("Central A", "50"))
    asset_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
        VALUES (?, 'FusionSolar', 'S1', 'Central A', 1)
        """,
        (asset_id,),
    )
    conn.commit()
    return conn, asset_id


def add_record(conn, asset_id: int, period_type: str, period_date: date, production: float | None, specific: float | None = None) -> None:
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type=period_type,
        period_date=period_date,
        production_kwh=production,
        specific_yield=specific if specific is not None else (production / 50 if production is not None else None),
        expected_kwh=None,
        expected_specific_yield=None,
        deviation_pct=None,
        performance_status="Sem referência",
        expected_source="none",
        data_quality="ok" if production is not None else "missing_production",
        notes="",
        payload_json="{}",
    )


def test_recalculation_creates_expected_values_after_historical_daily_records_exist(tmp_path) -> None:
    conn, asset_id = make_conn(tmp_path)
    try:
        add_record(conn, asset_id, "day", date(2025, 5, 4), 100, 2)
        add_record(conn, asset_id, "day", date(2024, 5, 4), 120, 2.4)
        add_record(conn, asset_id, "day", date(2026, 5, 4), 90, 1.8)

        summary = recalculate_performance_references(conn, period_type="day", period_date=date(2026, 5, 4))
        row = conn.execute("SELECT * FROM production_records WHERE period_date = '2026-05-04'").fetchone()

        assert summary["references_created"] == 1
        assert row["expected_specific_yield"] == 2.2
        assert row["expected_kwh"] == 110
        assert row["expected_source"] == "historical_same_period"
    finally:
        conn.close()


def test_recalculation_creates_mtd_expected_values_from_historical_daily_sums(tmp_path) -> None:
    conn, asset_id = make_conn(tmp_path)
    try:
        for year in (2025, 2024):
            add_record(conn, asset_id, "day", date(year, 5, 1), 50, 1)
            add_record(conn, asset_id, "day", date(year, 5, 2), 60, 1.2)
            add_record(conn, asset_id, "day", date(year, 5, 3), 70, 1.4)
            add_record(conn, asset_id, "day", date(year, 5, 4), 80, 1.6)
        add_record(conn, asset_id, "mtd", date(2026, 5, 1), 240, 4.8)

        summary = recalculate_performance_references(
            conn,
            period_type="mtd",
            period_date=date(2026, 5, 1),
            today_value=date(2026, 5, 4),
        )
        row = conn.execute("SELECT * FROM production_records WHERE period_type = 'mtd'").fetchone()

        assert summary["references_created"] == 1
        assert row["expected_kwh"] == 260
        assert row["expected_specific_yield"] == 5.2
    finally:
        conn.close()


def test_recalculation_creates_monthly_expected_values_from_historical_monthly_records(tmp_path) -> None:
    conn, asset_id = make_conn(tmp_path)
    try:
        add_record(conn, asset_id, "month", date(2025, 5, 1), 1000, 20)
        add_record(conn, asset_id, "month", date(2024, 5, 1), 1200, 24)
        add_record(conn, asset_id, "month", date(2026, 5, 1), 900, 18)

        recalculate_performance_references(conn, period_type="month", period_date=date(2026, 5, 1))
        row = conn.execute("SELECT * FROM production_records WHERE period_date = '2026-05-01' AND period_type = 'month'").fetchone()

        assert row["expected_specific_yield"] == 22
        assert row["expected_kwh"] == 1100
    finally:
        conn.close()


def test_recalculation_leaves_no_reference_when_no_history_exists(tmp_path) -> None:
    conn, asset_id = make_conn(tmp_path)
    try:
        add_record(conn, asset_id, "day", date(2026, 5, 4), 90, 1.8)

        summary = recalculate_performance_references(conn, period_type="day", period_date=date(2026, 5, 4))
        row = conn.execute("SELECT * FROM production_records WHERE period_date = '2026-05-04'").fetchone()
        diagnostic = json.loads(row["reference_diagnostic_json"])

        assert summary["still_without_reference"] == 1
        assert row["performance_status"] == "Sem referência"
        assert "No historical daily records found" in diagnostic["no_reference_reason"]
    finally:
        conn.close()


def test_backfill_triggers_reference_recalculation(tmp_path, monkeypatch) -> None:
    conn, asset_id = make_conn(tmp_path)
    try:
        add_record(conn, asset_id, "day", date(2025, 1, 2), 100, 2)
        monkeypatch.setattr(app_module, "get_fusionsolar_session", lambda _config: (object(), "token"))
        monkeypatch.setattr(
            app_module,
            "fetch_fusionsolar_kpi_day_rows",
            lambda _session, _base_url, _endpoint, station_codes, collect_date: [
                {
                    "stationCode": "S1",
                    "collectTime": app_module.collect_time_ms(date(2026, 1, 2)),
                    "dataItemMap": {"PVYield": "90"},
                    "payload_json": "{}",
                }
            ],
        )
        monkeypatch.setattr(
            app_module,
            "fetch_fusionsolar_kpi_month_map",
            lambda _session, _base_url, _endpoint, station_codes, collect_date: {
                "S1": {"stationCode": "S1", "dataItemMap": {"PVYield": "900"}, "payload_json": "{}"}
            },
        )

        result = run_fusionsolar_production_backfill(
            conn,
            period_type="day",
            from_year=2026,
            to_year=2026,
            today_value=date(2026, 1, 3),
            kpi_call_delay_seconds=0,
        )
        row = conn.execute("SELECT * FROM production_records WHERE period_date = '2026-01-02'").fetchone()

        assert result["references_created"] >= 1
        assert row["expected_specific_yield"] == 2
    finally:
        conn.close()


def test_diagnostic_reason_is_stored_when_no_reference_exists(tmp_path) -> None:
    conn, asset_id = make_conn(tmp_path)
    try:
        add_record(conn, asset_id, "mtd", date(2026, 5, 1), 200, 4)
        recalculate_performance_references(conn, period_type="mtd", period_date=date(2026, 5, 1), today_value=date(2026, 5, 4))
        row = conn.execute("SELECT reference_diagnostic_json FROM production_records WHERE period_type = 'mtd'").fetchone()
        diagnostic = json.loads(row["reference_diagnostic_json"])
        assert diagnostic["no_reference_reason"] == "MTD reference requires historical daily records for same period"
    finally:
        conn.close()
