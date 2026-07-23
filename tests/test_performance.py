from __future__ import annotations

import json
from datetime import date

import pytest

from app import (
    build_local_customer_production_report,
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
from monitoring_board.reporting.periods import period_from_form


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


def test_individual_monthly_report_keeps_partial_production_as_diagnostic(conn) -> None:
    asset_id = _insert_customer_asset(conn, "Central Parcial")
    _insert_customer_record(conn, asset_id, "day", date(2026, 4, 1), 10, 6, 4, 20)
    conn.commit()

    report = build_local_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-04",
        electricity_price=0.2,
        sell_price=0.05,
        reference_date=date(2026, 5, 1),
    )

    assert report["production_kwh"] is None
    assert report["raw_daily_total_kwh"] == 10
    assert report["production_quality_status"] == "partial"
    assert report["production_is_final"] is False
    assert report["net_benefit_eur"] is None
    assert any("Rascunho — produção incompleta: 1/30 dias disponíveis" in note for note in report["report_notes"])


def test_current_month_does_not_trigger_api_fallback_only_for_being_in_progress(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = _insert_customer_asset(conn, "Central Em Curso")
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-07",
        electricity_price=0.2,
        sell_price=0.05,
        reference_date=date(2026, 7, 22),
    )

    assert report["production_quality_status"] == "in_progress"
    assert report["production_kwh"] is None
    assert report["months_requiring_fallback"] == []


def _insert_customer_asset(conn, name: str = "Central Periodos") -> int:
    conn.execute(
        """
        INSERT INTO assets (project_name, installation_group, active_contract, kwp, location)
        VALUES (?, ?, 'yes', '50', 'Porto')
        """,
        (name, name),
    )
    asset_id = int(conn.execute("SELECT id FROM assets WHERE project_name = ?", (name,)).fetchone()["id"])
    conn.execute(
        """
        INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
        VALUES (?, 'FusionSolar', 'S1', ?, 1)
        """,
        (asset_id, f"{name} FS"),
    )
    return asset_id


def _insert_customer_record(conn, asset_id: int, period_type: str, period_date: date, production: float, self_use: float, export: float, consumption: float) -> None:
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type=period_type,
        period_date=period_date,
        production_kwh=production,
        specific_yield=None,
        expected_kwh=None,
        expected_specific_yield=None,
        deviation_pct=None,
        performance_status="OK",
        expected_source="none",
        data_quality="ok",
        notes="",
        payload_json=json.dumps(
            {
                "collectTime": 1767225600000,
                "dataItemMap": {
                    "PVYield": str(production),
                    "selfUsePower": str(self_use),
                    "ongrid_power": str(export),
                    "use_power": str(consumption),
                },
            }
        ),
    )


def test_quarterly_customer_report_sums_three_monthly_records(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = _insert_customer_asset(conn, "Central Trimestre")
    for month, production in [(1, 100), (2, 120), (3, 140)]:
        _insert_customer_record(conn, asset_id, "month", date(2026, month, 1), production, production - 20, 20, 200)
    conn.commit()
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.2,
        sell_price=0.05,
        period=period_from_form({"period_type": "quarterly", "report_year": "2026", "report_quarter": "1"}),
    )

    assert report["period_type"] == "quarterly"
    assert report["months_count"] == 3
    assert report["production_kwh"] == 360
    assert report["self_use_kwh"] == 300
    assert report["export_kwh"] == 60
    assert report["consumption_kwh"] == 600
    assert report["months_with_data"] == ["2026-01", "2026-02", "2026-03"]
    assert report["missing_months"] == []
    assert report["coverage_pct"] == 100
    assert report["chart_granularity"] == "monthly"


def test_semiannual_customer_report_mixes_monthly_and_daily_without_double_counting(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = _insert_customer_asset(conn, "Central Semestre")
    _insert_customer_record(conn, asset_id, "month", date(2026, 1, 1), 100, 70, 30, 180)
    _insert_customer_record(conn, asset_id, "day", date(2026, 1, 2), 10, 6, 4, 20)
    _insert_customer_record(conn, asset_id, "day", date(2026, 2, 1), 20, 15, 5, 30)
    _insert_customer_record(conn, asset_id, "day", date(2026, 2, 2), 30, 20, 10, 40)
    conn.commit()

    report = build_local_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.2,
        sell_price=0.05,
        period=period_from_form({"period_type": "semiannual", "report_year": "2026", "report_semester": "1"}),
    )

    assert report["production_kwh"] is None
    assert report["raw_daily_total_kwh"] == 60
    assert report["production_quality_status"] == "partial"
    assert report["months_with_data"] == ["2026-01"]
    assert report["missing_months"] == ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    assert report["coverage_pct"] == pytest.approx(16.67)
    assert any("Rascunho — produção incompleta" in note for note in report["report_notes"])


def test_annual_customer_report_identifies_missing_months(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = _insert_customer_asset(conn, "Central Anual")
    _insert_customer_record(conn, asset_id, "month", date(2026, 1, 1), 100, 60, 40, 180)
    _insert_customer_record(conn, asset_id, "month", date(2026, 12, 1), 200, 150, 50, 260)
    conn.commit()

    report = build_local_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.2,
        sell_price=0.05,
        period=period_from_form({"period_type": "annual", "report_year": "2026"}),
        reference_date=date(2027, 1, 1),
    )

    assert report["months_count"] == 12
    assert report["production_kwh"] is None
    assert report["months_with_data"] == ["2026-01", "2026-12"]
    assert report["missing_months"] == [
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
        "2026-10",
        "2026-11",
    ]
    assert report["coverage_pct"] == pytest.approx(16.67)


def test_quarterly_customer_report_fetches_only_missing_month_from_api(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = _insert_customer_asset(conn, "Central Fallback")
    _insert_customer_record(conn, asset_id, "month", date(2026, 1, 1), 100, 70, 30, 180)
    _insert_customer_record(conn, asset_id, "day", date(2026, 2, 1), 20, 15, 5, 30)
    _insert_customer_record(conn, asset_id, "day", date(2026, 2, 2), 30, 20, 10, 40)
    conn.execute(
        """
        INSERT INTO integration_configs (
            provider, enabled, base_url, username, password,
            day_kpi_endpoint, month_kpi_endpoint, created_at, updated_at
        )
        VALUES ('FusionSolar', 1, 'https://example.test', 'u', 'p', '/day', '/month', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """
    )
    conn.commit()
    fetched_months: list[date] = []

    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: (object(), {}))
    monkeypatch.setattr(
        "app.get_fusionsolar_endpoint_config",
        lambda _config: {"base_url": "https://example.test", "day_kpi_endpoint": "/day", "month_kpi_endpoint": "/month"},
    )
    monkeypatch.setattr("app.fetch_fusionsolar_kpi_day_rows", lambda *_args, **kwargs: [])

    def fake_month_map(*_args, **kwargs):
        collect_date = kwargs["collect_date"]
        fetched_months.append(collect_date)
        if collect_date == date(2026, 3, 1):
            return {
                "S1": {
                    "stationCode": "S1",
                    "dataItemMap": {
                        "PVYield": "90",
                        "selfUsePower": "50",
                        "ongrid_power": "40",
                        "use_power": "120",
                    },
                }
            }
        return {}

    monkeypatch.setattr("app.fetch_fusionsolar_kpi_month_map", fake_month_map)

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.2,
        sell_price=0.05,
        period=period_from_form({"period_type": "quarterly", "report_year": "2026", "report_quarter": "1"}),
    )

    queued_jobs = conn.execute(
        """
        SELECT params_json
        FROM background_jobs
        WHERE job_type = 'fusionsolar_report_production_request'
        ORDER BY id
        """
    ).fetchall()

    assert fetched_months == []
    assert report["production_kwh"] is None
    assert report["raw_daily_total_kwh"] == 50
    assert report["production_is_final"] is False
    assert report["production_refresh_queued"] is True
    assert len(queued_jobs) == 2


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
