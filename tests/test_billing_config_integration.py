from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import app as flask_app
from app import build_fusionsolar_customer_production_report, ensure_database, upsert_production_record
from monitoring_board.customer_reports import build_customer_report_pdf
from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, BillingMode, ReportType
from monitoring_board.reporting.periods import period_from_form
from monitoring_board.reporting.repositories import (
    ensure_billing_config_schema,
    get_asset_billing_config,
    get_asset_billing_config_row,
    upsert_asset_billing_config,
)


def connect(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "billing.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_report_asset(conn: sqlite3.Connection, *, contract_type: str = "ESCO", name: str = "Central Billing") -> int:
    conn.execute(
        "INSERT INTO assets (project_name, active_contract, contract_type, kwp) VALUES (?, 'yes', ?, '50')",
        (name, contract_type),
    )
    asset_id = int(conn.execute("SELECT id FROM assets WHERE project_name = ?", (name,)).fetchone()["id"])
    conn.execute(
        "INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled) VALUES (?, 'FusionSolar', ?, ?, 1)",
        (asset_id, f"S{asset_id}", f"{name} FS"),
    )
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id=f"S{asset_id}",
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
        payload_json=json.dumps({"collectTime": 1767225600000, "dataItemMap": {"PVYield": "100", "selfUsePower": "60", "ongrid_power": "40", "use_power": "120"}}),
    )
    conn.commit()
    return asset_id


def add_monthly_report_record(
    conn: sqlite3.Connection,
    asset_id: int,
    month: int,
    *,
    production: float = 100,
    self_use: float = 60,
    export: float = 40,
    consumption: float = 120,
    year: int = 2026,
) -> None:
    upsert_production_record(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        external_id=f"S{asset_id}",
        period_type="month",
        period_date=date(year, month, 1),
        production_kwh=production,
        specific_yield=2,
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


def billing_config(
    *,
    report_type: ReportType = ReportType.ESCO,
    mode: BillingMode = BillingMode.ENERGY,
    base: BillingEnergyBase = BillingEnergyBase.SELF_CONSUMPTION,
    solcor_price: str = "0.10",
    fixed_fee: str = "25",
    electricity_price: str = "0.20",
    export_price: str = "0.05",
) -> BillingConfig:
    return BillingConfig(
        report_type=report_type,
        billing_mode=mode,
        billing_energy_base=base,
        solcor_price_per_kwh=Decimal(solcor_price),
        fixed_monthly_fee_eur=Decimal(fixed_fee),
        electricity_price_eur_kwh=Decimal(electricity_price),
        export_price_eur_kwh=Decimal(export_price),
    )


def csrf_client(db_path: Path):
    flask_app.config["DATABASE"] = str(db_path)
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "test-token"
    return client


def test_billing_config_schema_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    ensure_billing_config_schema(conn)
    ensure_billing_config_schema(conn)

    table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'asset_billing_configs'").fetchone()
    assert table is not None


def test_save_update_and_read_billing_config(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    asset_id = add_report_asset(conn)

    upsert_asset_billing_config(conn, asset_id=asset_id, config=billing_config(solcor_price="0.10"))
    upsert_asset_billing_config(conn, asset_id=asset_id, config=billing_config(solcor_price="0.12", base=BillingEnergyBase.TOTAL_PRODUCTION))
    conn.commit()

    row = get_asset_billing_config_row(conn, asset_id)
    config = get_asset_billing_config(conn, asset_id, ReportType.ESCO)

    assert row is not None
    assert conn.execute("SELECT COUNT(*) FROM asset_billing_configs WHERE asset_id = ?", (asset_id,)).fetchone()[0] == 1
    assert config.solcor_price_per_kwh == Decimal("0.12")
    assert config.billing_energy_base is BillingEnergyBase.TOTAL_PRODUCTION


def test_missing_billing_config_returns_safe_defaults(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    asset_id = add_report_asset(conn)

    config = get_asset_billing_config(conn, asset_id, ReportType.ESCO)

    assert config.billing_mode is BillingMode.ENERGY
    assert config.billing_energy_base is BillingEnergyBase.SELF_CONSUMPTION
    assert config.solcor_price_per_kwh == Decimal("0")


def test_billing_configs_are_isolated_between_assets(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    asset_a = add_report_asset(conn, name="A")
    asset_b = add_report_asset(conn, name="B")
    upsert_asset_billing_config(conn, asset_id=asset_a, config=billing_config(solcor_price="0.11"))
    upsert_asset_billing_config(conn, asset_id=asset_b, config=billing_config(solcor_price="0.22"))
    conn.commit()

    assert get_asset_billing_config(conn, asset_a, ReportType.ESCO).solcor_price_per_kwh == Decimal("0.11")
    assert get_asset_billing_config(conn, asset_b, ReportType.ESCO).solcor_price_per_kwh == Decimal("0.22")


def test_billing_config_requires_existing_asset(tmp_path: Path) -> None:
    conn = connect(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        upsert_asset_billing_config(conn, asset_id=99999, config=billing_config())


def test_customer_report_uses_saved_billing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path)
    asset_id = add_report_asset(conn)
    saved = billing_config(solcor_price="0.10", electricity_price="0.20", export_price="0.05")
    upsert_asset_billing_config(conn, asset_id=asset_id, config=saved)
    conn.commit()
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=get_asset_billing_config(conn, asset_id, ReportType.ESCO),
    )

    assert report["self_use_kwh"] == 60
    assert report["solcor_payment_eur"] == 6
    assert report["grid_import_kwh"] == 60


def test_manual_override_does_not_change_saved_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path)
    asset_id = add_report_asset(conn)
    upsert_asset_billing_config(conn, asset_id=asset_id, config=billing_config(solcor_price="0.10"))
    conn.commit()
    manual = billing_config(solcor_price="0.20", base=BillingEnergyBase.TOTAL_PRODUCTION)
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=manual,
    )

    assert report["solcor_payment_eur"] == 20
    assert get_asset_billing_config(conn, asset_id, ReportType.ESCO).solcor_price_per_kwh == Decimal("0.10")


def test_saved_billing_config_applies_to_quarterly_period_without_energy_multiplier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path)
    asset_id = add_report_asset(conn)
    add_monthly_report_record(conn, asset_id, 2, production=120, self_use=70, export=50, consumption=130)
    add_monthly_report_record(conn, asset_id, 3, production=140, self_use=80, export=60, consumption=150)
    saved = billing_config(solcor_price="0.10", electricity_price="0.20", export_price="0.05")
    upsert_asset_billing_config(conn, asset_id=asset_id, config=saved)
    conn.commit()
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=get_asset_billing_config(conn, asset_id, ReportType.ESCO),
        period=period_from_form({"period_type": "quarterly", "report_year": "2026", "report_quarter": "1"}),
    )

    assert report["months_count"] == 3
    assert report["self_use_kwh"] == 210
    assert report["solcor_payment_eur"] == 21
    assert report["solcor_payment_eur"] != 63
    assert report["export_revenue_eur"] == 7.5
    assert report["missing_months"] == []


def test_manual_override_applies_to_semiannual_period_without_saving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path)
    asset_id = add_report_asset(conn)
    for month in range(2, 7):
        add_monthly_report_record(conn, asset_id, month, production=100, self_use=60, export=40, consumption=120)
    upsert_asset_billing_config(conn, asset_id=asset_id, config=billing_config(solcor_price="0.10"))
    conn.commit()
    manual = billing_config(solcor_price="0.20", base=BillingEnergyBase.TOTAL_PRODUCTION)
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    report = build_fusionsolar_customer_production_report(
        conn,
        asset_id=asset_id,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=manual,
        period=period_from_form({"period_type": "semiannual", "report_year": "2026", "report_semester": "1"}),
    )

    assert report["months_count"] == 6
    assert report["production_kwh"] == 600
    assert report["solcor_payment_eur"] == 120
    assert get_asset_billing_config(conn, asset_id, ReportType.ESCO).solcor_price_per_kwh == Decimal("0.10")


def test_annual_fixed_monthly_fee_uses_twelve_months_and_epc_stays_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path)
    esco_asset = add_report_asset(conn, contract_type="ESCO", name="ESCO Annual")
    epc_asset = add_report_asset(conn, contract_type="EPC", name="EPC Annual")
    for month in range(2, 13):
        add_monthly_report_record(conn, esco_asset, month)
        add_monthly_report_record(conn, epc_asset, month)
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    annual_period = period_from_form({"period_type": "annual", "report_year": "2026"})
    esco = build_fusionsolar_customer_production_report(
        conn,
        asset_id=esco_asset,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=billing_config(mode=BillingMode.FIXED_MONTHLY_FEE, fixed_fee="33"),
        period=annual_period,
    )
    epc = build_fusionsolar_customer_production_report(
        conn,
        asset_id=epc_asset,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=billing_config(report_type=ReportType.EPC, mode=BillingMode.FIXED_MONTHLY_FEE, fixed_fee="999"),
        period=annual_period,
    )

    assert esco["solcor_payment_eur"] == 396
    assert epc["solcor_payment_eur"] == 0
    assert esco["missing_months"] == []
    assert epc["missing_months"] == []


def test_report_supports_total_production_fixed_fee_warnings_epc_and_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path)
    esco_asset = add_report_asset(conn, contract_type="ESCO", name="ESCO PDF")
    epc_asset = add_report_asset(conn, contract_type="EPC", name="EPC PDF")
    monkeypatch.setattr("app.get_fusionsolar_session", lambda _config: pytest.fail("API should not be called"))

    total_production = build_fusionsolar_customer_production_report(
        conn,
        asset_id=esco_asset,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=billing_config(base=BillingEnergyBase.TOTAL_PRODUCTION, solcor_price="0.10"),
    )
    fixed_fee = build_fusionsolar_customer_production_report(
        conn,
        asset_id=esco_asset,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=billing_config(mode=BillingMode.FIXED_MONTHLY_FEE, fixed_fee="33"),
    )
    missing_prices = build_fusionsolar_customer_production_report(
        conn,
        asset_id=esco_asset,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=billing_config(solcor_price="0", electricity_price="0", export_price="0"),
    )
    epc = build_fusionsolar_customer_production_report(
        conn,
        asset_id=epc_asset,
        report_month="2026-01",
        electricity_price=0.0,
        sell_price=0.0,
        billing_config=billing_config(report_type=ReportType.EPC, solcor_price="9"),
    )

    assert total_production["solcor_payment_eur"] == 10
    assert fixed_fee["solcor_payment_eur"] == 33
    assert "missing_solcor_price" in missing_prices["billing_warnings"]
    assert epc["solcor_payment_eur"] == 0
    assert build_customer_report_pdf(total_production).startswith(b"%PDF-")


def test_exports_selection_loads_selected_asset_billing_config_and_keeps_isolation(tmp_path: Path) -> None:
    db_path = tmp_path / "route-billing.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    asset_a = add_report_asset(conn, name="Asset A")
    asset_b = add_report_asset(conn, name="Asset B")
    upsert_asset_billing_config(conn, asset_id=asset_a, config=billing_config(solcor_price="0.11", electricity_price="0.21", export_price="0.04"))
    upsert_asset_billing_config(
        conn,
        asset_id=asset_b,
        config=billing_config(
            mode=BillingMode.FIXED_MONTHLY_FEE,
            base=BillingEnergyBase.TOTAL_PRODUCTION,
            solcor_price="0.22",
            fixed_fee="77",
            electricity_price="0.31",
            export_price="0.08",
        ),
    )
    conn.commit()
    conn.close()

    client = csrf_client(db_path)
    response = client.get(f"/exports?asset_id={asset_b}&period_type=quarterly&report_year=2026&report_quarter=2")
    html = response.data.decode()

    assert response.status_code == 200
    assert 'value="0.22"' in html
    assert 'value="77"' in html
    assert 'value="0.31"' in html
    assert 'value="0.08"' in html
    assert '<option value="fixed_monthly_fee" selected>' in html
    assert '<option value="quarterly" selected>' in html
    assert '<option value="2" selected>T2' in html
    assert 'name="asset_id" id="report-asset"' in html

    save_b = client.post(
        "/exports",
        data={
            "csrf_token": "test-token",
            "action": "save_billing_config",
            "asset_id": str(asset_b),
            "report_month": "2026-01",
            "billing_values_source": "manual",
            "billing_mode": "energy",
            "billing_energy_base": "self_consumption",
            "solcor_price_per_kwh": "0.44",
            "fixed_monthly_fee_eur": "0",
            "electricity_price": "0.55",
            "sell_price": "0.09",
        },
    )
    assert save_b.status_code in {302, 303}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    config_a = get_asset_billing_config(conn, asset_a, ReportType.ESCO)
    config_b = get_asset_billing_config(conn, asset_b, ReportType.ESCO)
    conn.close()
    assert config_a.solcor_price_per_kwh == Decimal("0.11")
    assert config_b.solcor_price_per_kwh == Decimal("0.44")


@pytest.mark.parametrize(
    "payload",
    [
        {"asset_id": "99999"},
        {"billing_mode": "bad-mode"},
        {"billing_energy_base": "bad-base"},
        {"billing_values_source": "bad-source"},
        {"solcor_price_per_kwh": "abc"},
        {"solcor_price_per_kwh": "NaN"},
        {"solcor_price_per_kwh": "-1"},
    ],
)
def test_exports_rejects_invalid_billing_inputs(tmp_path: Path, payload: dict[str, str]) -> None:
    db_path = tmp_path / "invalid-billing.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    asset_id = add_report_asset(conn)
    conn.close()
    data = {
        "csrf_token": "test-token",
        "action": "save_billing_config",
        "asset_id": str(asset_id),
        "report_month": "2026-01",
        "billing_values_source": "manual",
        "billing_mode": "energy",
        "billing_energy_base": "self_consumption",
        "solcor_price_per_kwh": "0.10",
        "fixed_monthly_fee_eur": "0",
        "electricity_price": "0.20",
        "sell_price": "0.05",
    }
    data.update(payload)

    response = csrf_client(db_path).post("/exports", data=data)

    assert response.status_code in {302, 303}
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM asset_billing_configs").fetchone()[0]
    conn.close()
    assert count == 0


def test_exports_rejects_unknown_asset_before_generating_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "unknown-asset-generate.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    add_report_asset(conn)
    conn.close()
    monkeypatch.setattr("app.build_fusionsolar_customer_production_report", lambda *args, **kwargs: pytest.fail("Report should not be generated"))

    response = csrf_client(db_path).post(
        "/exports",
        data={
            "csrf_token": "test-token",
            "action": "generate_report",
            "asset_id": "99999",
            "report_month": "2026-01",
            "billing_values_source": "saved",
            "billing_mode": "energy",
            "billing_energy_base": "self_consumption",
            "solcor_price_per_kwh": "0.10",
            "fixed_monthly_fee_eur": "0",
            "electricity_price": "0.20",
            "sell_price": "0.05",
        },
    )

    assert response.status_code in {302, 303}


def test_exports_rejects_asset_without_report_integration(tmp_path: Path) -> None:
    db_path = tmp_path / "no-integration.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO assets (project_name, contract_type) VALUES ('No Integration', 'ESCO')")
    asset_id = int(conn.execute("SELECT id FROM assets").fetchone()[0])
    conn.commit()
    conn.close()

    response = csrf_client(db_path).post(
        "/exports",
        data={
            "csrf_token": "test-token",
            "action": "save_billing_config",
            "asset_id": str(asset_id),
            "report_month": "2026-01",
            "billing_values_source": "manual",
            "billing_mode": "energy",
            "billing_energy_base": "self_consumption",
            "solcor_price_per_kwh": "0.10",
            "fixed_monthly_fee_eur": "0",
            "electricity_price": "0.20",
            "sell_price": "0.05",
        },
    )

    assert response.status_code in {302, 303}
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM asset_billing_configs").fetchone()[0]
    conn.close()
    assert count == 0


def test_exports_accepts_valid_manual_input(tmp_path: Path) -> None:
    db_path = tmp_path / "valid-billing.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    asset_id = add_report_asset(conn)
    conn.close()

    response = csrf_client(db_path).post(
        "/exports",
        data={
            "csrf_token": "test-token",
            "action": "save_billing_config",
            "asset_id": str(asset_id),
            "report_month": "2026-01",
            "billing_values_source": "manual",
            "billing_mode": "energy",
            "billing_energy_base": "self_consumption",
            "solcor_price_per_kwh": "0.10",
            "fixed_monthly_fee_eur": "",
            "electricity_price": "0.20",
            "sell_price": "0.05",
        },
    )

    assert response.status_code in {302, 303}
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM asset_billing_configs").fetchone()[0] == 1
    conn.close()
