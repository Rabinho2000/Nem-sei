from __future__ import annotations

import json
from datetime import date

import pytest

from app import (
    build_fusionsolar_customer_production_report,
    build_production_report_rows,
    build_missing_production_note,
    calculate_expected_production,
    calculate_specific_yield,
    classify_performance_status,
    compute_performance_percentage,
    ensure_database,
    format_number,
    parse_kwp_value,
    select_production_kwh,
    select_production_value,
    upsert_production_record,
)
from monitoring_board.db import get_db


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "performance.db"
    ensure_database(str(db_path))
    connection = get_db(str(db_path))
    try:
        yield connection
    finally:
        connection.close()


def test_parse_kwp_text_values() -> None:
    assert parse_kwp_value("61.6") == 61.6
    assert parse_kwp_value("61,6") == 61.6
    assert parse_kwp_value("") is None
    assert parse_kwp_value("-") is None


def test_select_production_value_prefers_pvyield() -> None:
    assert select_production_kwh({"PVYield": "10", "inverterYield": "20", "inverter_power": "30"}) == 10
    assert select_production_kwh({"inverterYield": "20", "inverter_power": "30"}) == 20
    assert select_production_kwh({"inverter_power": "30"}) == 30
    assert select_production_kwh({}) is None


def test_select_production_value_returns_selected_key() -> None:
    assert select_production_value({"PVYield": "10", "inverterYield": "20"}) == (10, "PVYield", "10")
    assert select_production_value({"inverterYield": "20"}) == (20, "inverterYield", "20")
    assert select_production_value({"inverter_power": "30"}) == (30, "inverter_power", "30")
    assert select_production_value({"theory_power": "40"}) == (None, "", "")


def test_missing_production_note_lists_available_keys() -> None:
    note = build_missing_production_note(
        {"theory_power": 1, "installed_capacity": 2, "perpower_ratio": 3},
        station_code="S1",
        period_type="day",
        period_date=date(2026, 5, 3),
    )
    assert "No production key found" in note
    assert "installed_capacity" in note
    assert "perpower_ratio" in note
    assert "theory_power" in note
    assert "stationCode=S1" in note


def test_specific_yield_calculation() -> None:
    assert calculate_specific_yield(123.2, 61.6) == 2.0
    assert calculate_specific_yield(None, 61.6) is None
    assert calculate_specific_yield(100, None) is None


def test_compute_performance_percentage() -> None:
    assert compute_performance_percentage({"specific_yield": 82, "expected_specific_yield": 100}) == 82
    assert compute_performance_percentage({"specific_yield": 108, "expected_specific_yield": 100}) == 108


def test_compute_performance_percentage_no_reference_or_data() -> None:
    assert compute_performance_percentage({"specific_yield": 82, "expected_specific_yield": None}) is None
    assert compute_performance_percentage({"specific_yield": None, "expected_specific_yield": 100}) is None
    assert compute_performance_percentage({"specific_yield": 82, "expected_specific_yield": 0}) is None


def test_format_number_trims_float_artifacts() -> None:
    assert format_number(820.8000000000001, 2) == "820.8"
    assert format_number(61.6000, 2) == "61.6"
    assert format_number(12.345, 2) == "12.35"
    assert format_number(None, 2) == "-"


def test_monthly_production_report_uses_monthly_api_records(conn) -> None:
    conn.execute(
        """
        INSERT INTO assets (project_name, installation_group, active_contract, kwp, location)
        VALUES ('Central Producao', 'Central Producao', 'yes', '50', 'Lisboa')
        """
    )
    asset_id = conn.execute("SELECT id FROM assets WHERE project_name = 'Central Producao'").fetchone()["id"]
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type="month",
        period_date=date(2026, 1, 1),
        production_kwh=1000,
        specific_yield=20,
        expected_kwh=900,
        expected_specific_yield=18,
        deviation_pct=11.11,
        performance_status="OK",
        expected_source="budget",
        data_quality="ok",
        notes="",
        payload_json="{}",
    )
    conn.commit()

    rows = build_production_report_rows(
        conn,
        {"period": "month", "report_month": "2026-01", "source": "FusionSolar", "om_only": "yes"},
    )

    assert len(rows) == 1
    assert rows[0]["project_name"] == "Central Producao"
    assert rows[0]["production_kwh"] == 1000
    assert rows[0]["specific_yield"] == 20
    assert rows[0]["data_source"] == "KPI mensal API"


def test_annual_production_report_falls_back_to_daily_records(conn) -> None:
    conn.execute(
        """
        INSERT INTO assets (project_name, installation_group, active_contract, kwp)
        VALUES ('Central Diaria', 'Central Diaria', 'yes', '10')
        """
    )
    asset_id = conn.execute("SELECT id FROM assets WHERE project_name = 'Central Diaria'").fetchone()["id"]
    for day, production in [(1, 10), (2, 15)]:
        upsert_production_record(
            conn,
            asset_id=asset_id,
            provider="FusionSolar",
            external_id="S2",
            period_type="day",
            period_date=date(2026, 2, day),
            production_kwh=production,
            specific_yield=production / 10,
            expected_kwh=10,
            expected_specific_yield=1,
            deviation_pct=0,
            performance_status="OK",
            expected_source="history",
            data_quality="ok",
            notes="",
            payload_json="{}",
        )
    conn.commit()

    rows = build_production_report_rows(
        conn,
        {"period": "year", "report_year": "2026", "source": "FusionSolar", "om_only": "yes"},
    )

    assert rows[0]["project_name"] == "Central Diaria"
    assert rows[0]["production_kwh"] == 25
    assert rows[0]["data_points"] == 2
    assert rows[0]["data_source"] == "KPI diario API"


def test_customer_pdf_report_uses_local_production_records(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    conn.execute(
        """
        INSERT INTO assets (project_name, installation_group, active_contract, kwp, location)
        VALUES ('Central Cliente', 'Central Cliente', 'yes', '50', 'Porto')
        """
    )
    asset_id = conn.execute("SELECT id FROM assets WHERE project_name = 'Central Cliente'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
        VALUES (?, 'FusionSolar', 'S1', 'Central Cliente FS', 1)
        """,
        (asset_id,),
    )
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type="day",
        period_date=date(2026, 1, 1),
        production_kwh=10,
        specific_yield=0.2,
        expected_kwh=None,
        expected_specific_yield=None,
        deviation_pct=None,
        performance_status="OK",
        expected_source="none",
        data_quality="ok",
        notes="",
        payload_json=json.dumps({"collectTime": 1767225600000, "dataItemMap": {"PVYield": "10", "selfUsePower": "6", "ongrid_power": "4"}}),
    )
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type="month",
        period_date=date(2026, 1, 1),
        production_kwh=100,
        specific_yield=2,
        expected_kwh=None,
        expected_specific_yield=None,
        deviation_pct=None,
        performance_status="OK",
        expected_source="none",
        data_quality="ok",
        notes="",
        payload_json=json.dumps({"collectTime": 1767225600000, "dataItemMap": {"PVYield": "100", "selfUsePower": "60", "ongrid_power": "40"}}),
    )
    conn.commit()
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.2,
        sell_price=0.05,
    )

    assert report["data_source"] == "Dados locais"
    assert report["production_kwh"] == 100
    assert report["self_use_kwh"] == 60
    assert report["export_kwh"] == 40
    assert report["daily_rows"][0]["production_kwh"] == 10


def test_deviation_classification_with_thresholds() -> None:
    assert classify_performance_status(95, 10, 100)[0] == "OK"
    assert classify_performance_status(85, 10, 100)[0] == "Atenção"
    assert classify_performance_status(75, 10, 100)[0] == "Alerta"
    assert classify_performance_status(65, 10, 100)[0] == "Crítico"
    assert classify_performance_status(None, 10, 100)[0] == "Sem dados"
    assert classify_performance_status(100, None, 100)[0] == "Sem referência"
    assert classify_performance_status(100, 10, None)[0] == "Sem referência"


def test_upsert_production_records(conn) -> None:
    cursor = conn.execute("INSERT INTO assets (project_name, kwp) VALUES (?, ?)", ("Central A", "50"))
    asset_id = int(cursor.lastrowid)
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type="day",
        period_date=date(2026, 5, 3),
        production_kwh=100,
        specific_yield=2,
        expected_kwh=110,
        expected_specific_yield=2.2,
        deviation_pct=-9.09,
        performance_status="OK",
        expected_source="monthly_budget",
        data_quality="ok",
        notes="",
        payload_json="{}",
    )
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type="day",
        period_date=date(2026, 5, 3),
        production_kwh=90,
        specific_yield=1.8,
        expected_kwh=110,
        expected_specific_yield=2.2,
        deviation_pct=-18.18,
        performance_status="Atenção",
        expected_source="monthly_budget",
        data_quality="ok",
        notes="updated",
        payload_json='{"x": 1}',
    )
    row = conn.execute("SELECT COUNT(*) AS total, production_kwh, performance_status FROM production_records").fetchone()
    assert row["total"] == 1
    assert row["production_kwh"] == 90
    assert row["performance_status"] == "Atenção"


def test_baseline_calculation_from_historical_same_day(conn) -> None:
    cursor = conn.execute("INSERT INTO assets (project_name, kwp) VALUES (?, ?)", ("Central A", "50"))
    asset_id = int(cursor.lastrowid)
    for year, production in [(2025, 100), (2024, 120)]:
        upsert_production_record(
            conn,
            asset_id=asset_id,
            provider="FusionSolar",
            external_id="S1",
            period_type="day",
            period_date=date(year, 5, 3),
            production_kwh=production,
            specific_yield=production / 50,
            expected_kwh=None,
            expected_specific_yield=None,
            deviation_pct=None,
            performance_status="Sem referência",
            expected_source="none",
            data_quality="ok",
            notes="",
            payload_json="{}",
        )
    expected_kwh, expected_specific_yield, source, quality = calculate_expected_production(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        period_type="day",
        period_date=date(2026, 5, 3),
        kwp=50,
        settings={"baseline_years": 2, "min_baseline_points": 1, "monthly_budget_json": ""},
    )
    assert expected_kwh == 110
    assert expected_specific_yield == 2.2
    assert source == "historical_same_period"
    assert quality == "ok"


def test_no_baseline_returns_no_reference(conn) -> None:
    cursor = conn.execute("INSERT INTO assets (project_name, kwp) VALUES (?, ?)", ("Central A", "50"))
    asset_id = int(cursor.lastrowid)
    expected_kwh, expected_specific_yield, source, _quality = calculate_expected_production(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        period_type="day",
        period_date=date(2026, 5, 3),
        kwp=50,
        settings={"baseline_years": 2, "min_baseline_points": 1, "monthly_budget_json": ""},
    )
    status, _data_quality, _deviation = classify_performance_status(100, 50, expected_kwh)
    assert expected_specific_yield is None
    assert source == "none"
    assert status == "Sem referência"
