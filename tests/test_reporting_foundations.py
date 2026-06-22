from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
import inspect
from pathlib import Path

import pytest

import app as app_module
from app import ensure_database, upsert_production_record
from monitoring_board.reporting import availability, billing, degradation, models, periods, repositories
from monitoring_board.customer_reports import prepare_customer_report
from monitoring_board.portfolio_reports import (
    aggregate_portfolio_total,
    calculate_degradation_factor,
    export_portfolio_report_workbook,
)


def connect(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "reporting-foundations.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def sample_customer_report(contract_type: str) -> dict:
    return {
        "asset": {
            "asset_id": 1,
            "project_name": "Central Teste",
            "contract_type": contract_type,
            "asset_type": "",
            "coverage_type": "",
            "sell_to": "",
        },
        "month_start": date(2026, 1, 1),
        "month_end": date(2026, 1, 31),
        "month_label": "Janeiro 2026",
        "daily_rows": [],
        "production_kwh": 100,
        "self_use_kwh": 80,
        "export_kwh": 20,
        "consumption_kwh": 200,
        "electricity_price": 0.20,
        "sell_price": 0.05,
    }


def test_characterizes_current_esco_and_epc_financial_report_values() -> None:
    esco = prepare_customer_report(sample_customer_report("ESCO"), solcor_price_per_kwh=0.09)
    epc = prepare_customer_report(sample_customer_report("EPC"), solcor_price_per_kwh=0.09)

    assert esco["savings_eur"] == 16
    assert esco["export_revenue_eur"] == 1
    assert esco["total_benefit_eur"] == 17
    assert esco["solcor_payment_eur"] == 9
    assert esco["net_benefit_eur"] == 8
    assert epc["solcor_payment_eur"] == 0
    assert epc["net_benefit_eur"] == epc["total_benefit_eur"]


def test_characterizes_current_monthly_local_customer_report_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path)
    conn.execute("INSERT INTO assets (project_name, active_contract, kwp) VALUES ('Central Cliente', 'yes', '50')")
    asset_id = int(conn.execute("SELECT id FROM assets").fetchone()["id"])
    conn.execute(
        "INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled) VALUES (?, 'FusionSolar', 'S1', 'Central Cliente FS', 1)",
        (asset_id,),
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
        payload_json='{"collectTime": 1767225600000, "dataItemMap": {"PVYield": "100", "selfUsePower": "60", "ongrid_power": "40"}}',
    )
    conn.commit()
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = app_module.build_fusionsolar_customer_production_report(
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


def test_characterizes_current_portfolio_total_and_excel_shape() -> None:
    rows = [
        {
            "portfolio": "P",
            "sub_account": "001",
            "nif": "123",
            "external_installation": "A",
            "local_installation": "A",
            "actual_production_kwh": 100,
            "production_ponta_kwh": 1,
            "production_cheia_kwh": 2,
            "production_vazio_kwh": 3,
            "production_super_vazio_kwh": 4,
            "helioscope_expected_kwh": 110,
            "adjusted_expected_kwh": 100,
            "degradation_factor": 0.97,
            "deviation_kwh": 0,
            "deviation_pct": 0,
            "availability_pct": 90,
            "tariff_type": "simple",
            "estimated_value_eur": 10,
            "status_label": "OK",
            "warning_labels": ["OK"],
            "warnings": [],
        }
    ]

    total = aggregate_portfolio_total(rows)
    workbook = export_portfolio_report_workbook(rows + [total])
    sheet = workbook.active

    assert total["actual_production_kwh"] == 100
    assert total["availability_pct"] == 90
    assert sheet.title == "Portfolio report"
    assert sheet["A1"].value == "Portfolio"
    assert sheet["F1"].value == "Producao real mensal kWh"


def test_characterizes_current_wat_edge_tolerance_and_power_fallback() -> None:
    valid_slots = {datetime(2026, 6, 1, 8, 0) + timedelta(minutes=15 * index) for index in range(8)}
    result = app_module.calculate_inverter_daily_availability(
        [
            {"sample_time": datetime(2026, 6, 1, 8, 1), "active_power_kw": 10},
            {"sample_time": datetime(2026, 6, 1, 8, 16), "active_power_kw": 10},
            {"sample_time": datetime(2026, 6, 1, 8, 31), "active_power_kw": 0},
        ],
        valid_slots,
    )

    assert result == {"valid_slots": 4, "available_slots": 0, "unavailable_slots": 4, "availability_pct": 0.0}
    assert app_module.calculate_weighted_plant_availability(
        [
            {"availability_pct": 100.0, "inverter_power_kw": None},
            {"availability_pct": 50.0, "inverter_power_kw": 50.0},
        ]
    ) == 75.0


def test_characterizes_current_degradation_formula() -> None:
    assert calculate_degradation_factor(None, date(2026, 1, 1)) == 1.0
    assert calculate_degradation_factor(date(2026, 1, 1), date(2026, 1, 1)) == 0.975
    assert round(calculate_degradation_factor(date(2025, 1, 1), date(2026, 1, 1)), 4) == 0.9695


def test_reporting_models_and_periods_are_explicit_and_typed() -> None:
    period = periods.monthly_period("2026-02")

    assert models.ReportPeriodType.MONTHLY.value == "monthly"
    assert models.ReportType.ESCO.value == "esco"
    assert period.start == date(2026, 2, 1)
    assert period.end == date(2026, 2, 28)
    assert period.month_count == 1
    assert period.included_months == (date(2026, 2, 1),)
    assert periods.normalize_report_month("invalid", today=date(2026, 6, 22)) == "2026-06"
    assert periods.normalize_report_year("2099", today=date(2026, 6, 22)) == 2026


def test_billing_foundation_uses_decimal_and_distinguishes_esco_from_epc() -> None:
    energy = models.EnergyBreakdown(
        production_kwh=Decimal("100"),
        self_use_kwh=Decimal("80"),
        export_kwh=Decimal("20"),
        consumption_kwh=Decimal("200"),
    )
    esco = billing.calculate_customer_billing(
        energy,
        models.BillingConfig(
            report_type=models.ReportType.ESCO,
            solcor_price_per_kwh=Decimal("0.09"),
            electricity_price_eur_kwh=Decimal("0.20"),
            export_price_eur_kwh=Decimal("0.05"),
        ),
    )
    epc = billing.calculate_customer_billing(
        energy,
        models.BillingConfig(
            report_type=models.ReportType.EPC,
            solcor_price_per_kwh=Decimal("0.09"),
            electricity_price_eur_kwh=Decimal("0.20"),
            export_price_eur_kwh=Decimal("0.05"),
        ),
    )

    assert esco.savings_eur == Decimal("16.00")
    assert esco.export_revenue_eur == Decimal("1.00")
    assert esco.solcor_payment_eur == Decimal("9.00")
    assert epc.solcor_payment_eur == Decimal("0")
    assert billing.decimal_from_value(None) == Decimal("0")
    assert billing.decimal_from_value("-4.2") == Decimal("0")
    assert billing.detect_report_type_value({"contract_type": "Contrato ESCO"}) is models.ReportType.ESCO
    assert billing.infer_self_use_kwh(
        production_kwh=Decimal("10"),
        export_kwh=Decimal("3"),
        raw_self_use=None,
    ) == Decimal("7")


def test_availability_and_degradation_foundations_preserve_edge_cases() -> None:
    valid_slots = {datetime(2026, 6, 1, 8, 0) + timedelta(minutes=15 * index) for index in range(8)}

    assert availability.is_inverter_available(Decimal("0.1")) is True
    assert availability.is_inverter_available(0) is False
    assert availability.parse_nonnegative_float("-1") is None
    assert availability.calculate_inverter_daily_availability([], valid_slots)["valid_slots"] == 4
    assert availability.calculate_weighted_plant_availability(
        [
            {"availability_pct": 100.0, "inverter_power_kw": "100"},
            {"availability_pct": 50.0, "inverter_power_kw": "50"},
        ]
    ) == 83.33
    assert degradation.calculate_degradation_factor(None, date(2026, 1, 1)) == 1.0
    assert degradation.calculate_degradation_factor(date(2027, 1, 1), date(2026, 1, 1)) == 0.975


def test_reporting_repositories_read_existing_sqlite_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    group_names = [row["name"] for row in repositories.list_portfolio_groups(conn)]
    assert "Solcorelios I" in group_names

    conn.execute("INSERT INTO assets (project_name, active_contract, kwp) VALUES ('Central Repo', 'yes', '10')")
    asset_id = int(conn.execute("SELECT id FROM assets WHERE project_name = 'Central Repo'").fetchone()["id"])
    conn.execute(
        """
        INSERT INTO asset_tariffs (asset_id, tariff_type, simple_price_eur_kwh, valid_from, valid_to)
        VALUES (?, 'simple', 0.12, '2025-01-01', '2025-12-31')
        """,
        (asset_id,),
    )
    conn.execute(
        """
        INSERT INTO asset_tariffs (asset_id, tariff_type, simple_price_eur_kwh, valid_from, valid_to)
        VALUES (?, 'simple', 0.15, '2026-01-01', '')
        """,
        (asset_id,),
    )
    tariff_id = int(conn.execute("SELECT MAX(id) AS id FROM asset_tariffs").fetchone()["id"])
    conn.execute(
        "INSERT INTO tariff_period_rules (tariff_id, weekday_type, start_time, end_time, period_name) VALUES (?, 'all', '00:00', '23:59', 'cheia')",
        (tariff_id,),
    )
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id="S1",
        period_type="month",
        period_date=date(2026, 1, 1),
        production_kwh=123,
        specific_yield=None,
        expected_kwh=None,
        expected_specific_yield=None,
        deviation_pct=None,
        performance_status="OK",
        expected_source="none",
        data_quality="ok",
        notes="",
        payload_json="{}",
    )
    conn.commit()

    latest = repositories.get_latest_tariff(conn, asset_id, date(2026, 1, 1))
    assert latest is not None
    assert latest["simple_price_eur_kwh"] == 0.15
    assert repositories.has_expired_tariff(conn, asset_id, date(2026, 1, 1)) is True
    assert repositories.list_tariff_period_rules(conn, tariff_id)[0]["period_name"] == "cheia"
    assert repositories.get_monthly_production_record(conn, asset_id, date(2026, 1, 1))["production_kwh"] == 123
    assert repositories.get_monthly_production_record(conn, None, date(2026, 1, 1)) is None


def test_reporting_foundation_modules_do_not_import_flask() -> None:
    for module in (availability, billing, degradation, models, periods, repositories):
        assert "flask" not in inspect.getsource(module).lower()
