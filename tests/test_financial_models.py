from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from openpyxl import Workbook

from app import app, ensure_database
from monitoring_board.db import get_db
from monitoring_board.financial_model_repository import list_model_monthly
from monitoring_board.portfolio_reports import build_portfolio_report_rows
from monitoring_board.reporting.financial_models import FinancialModelParseError, parse_financial_model_workbook
from monitoring_board.services.financial_models import (
    FinancialModelError,
    confirm_financial_model_import,
    create_financial_model_preview,
)


class UploadStub:
    def __init__(self, path: Path, filename: str | None = None) -> None:
        self.path = path
        self.filename = filename or path.name

    def save(self, target: Path) -> None:
        target.write_bytes(self.path.read_bytes())


def connect(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "financial_models.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_asset(conn: sqlite3.Connection, *, name: str = "Usinage", nif: str = "505435748", kwp: str = "138.6") -> int:
    cursor = conn.execute(
        "INSERT INTO assets (project_name, nif, kwp, mounting_date, start_contract) VALUES (?, ?, ?, '2024-01-01', '2024-01-01')",
        (name, nif, kwp),
    )
    return int(cursor.lastrowid)


def financial_workbook(path: Path, *, sheet_name: str = "Prod month", unit_label: str = "PV Production (kWh)", missing_month: bool = False) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if missing_month:
        months = months[:-1]
    sheet.append(["Metric", *months])
    sheet.append([unit_label, *[month * 100 for month in range(1, len(months) + 1)]])
    sheet.append(["Consumption (kWh)", *[month * 120 for month in range(1, len(months) + 1)]])
    sheet.append(["Self consumption (kWh)", *[month * 80 for month in range(1, len(months) + 1)]])
    meta = workbook.create_sheet("UPAC")
    meta["A4"] = "Usinage"
    meta["D4"] = 138.6
    workbook.save(path)


def test_financial_model_schema_new_and_existing_db(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'financial_models'").fetchone()
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'financial_model_monthly'").fetchone()
        ensure_database(str(tmp_path / "financial_models.db"))
    finally:
        conn.close()


def test_parse_financial_model_workbook_xlsx_and_mwh_conversion(tmp_path: Path) -> None:
    path = tmp_path / "model.xlsx"
    financial_workbook(path, unit_label="PV Production (MWh)")

    parsed = parse_financial_model_workbook(path)

    assert parsed.detected_name == "Usinage"
    assert parsed.detected_kwp == 138.6
    assert len(parsed.monthly) == 12
    assert parsed.monthly[0]["expected_production_kwh"] == 100000
    assert parsed.monthly[0]["source_fields"]["expected_production_kwh"]["conversion"] == "mwh_to_kwh"
    assert parsed.monthly[0]["expected_export_kwh"] == 99920
    assert "financial_model_calculated_export" in parsed.monthly[0]["warnings"]


def test_parse_financial_model_workbook_xlsm(tmp_path: Path) -> None:
    path = tmp_path / "model.xlsm"
    financial_workbook(path)

    parsed = parse_financial_model_workbook(path)

    assert len(parsed.monthly) == 12
    assert parsed.monthly[11]["expected_production_kwh"] == 1200


def test_parse_financial_model_rejects_missing_month_and_ambiguous_sheets(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xlsx"
    financial_workbook(missing, missing_month=True)
    with pytest.raises(FinancialModelParseError):
        parse_financial_model_workbook(missing)

    ambiguous = tmp_path / "ambiguous.xlsx"
    financial_workbook(ambiguous)
    workbook = Workbook()
    first = workbook.active
    first.title = "One"
    first.append(["Metric", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    first.append(["PV Production", *range(1, 13)])
    second = workbook.create_sheet("Two")
    second.append(["Metric", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    second.append(["PV Production", *range(1, 13)])
    workbook.save(ambiguous)
    with pytest.raises(FinancialModelParseError):
        parse_financial_model_workbook(ambiguous)


def test_create_preview_confirm_version_and_duplicate_hash(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    try:
        asset_id = add_asset(conn)
        path = tmp_path / "model.xlsx"
        financial_workbook(path)
        model_id = create_financial_model_preview(conn, upload_dir=tmp_path / "uploads", file_storage=UploadStub(path), asset_id=asset_id, base_year=2026)
        assert len(list_model_monthly(conn, model_id=model_id)) == 12
        version = confirm_financial_model_import(conn, model_id=model_id, asset_id=asset_id)
        conn.commit()
        assert version == 1
        model = conn.execute("SELECT * FROM financial_models WHERE id = ?", (model_id,)).fetchone()
        assert model["status"] == "confirmed"
        assert model["active"] == 1
        with pytest.raises(FinancialModelError):
            create_financial_model_preview(conn, upload_dir=tmp_path / "uploads", file_storage=UploadStub(path), asset_id=asset_id, base_year=2026)
    finally:
        conn.close()


def test_financial_model_precedence_over_helioscope_and_year_isolated(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    try:
        asset_id = add_asset(conn)
        portfolio_id = conn.execute("SELECT id FROM portfolio_groups WHERE name = 'Solcorelios I'").fetchone()["id"]
        conn.execute("INSERT INTO portfolio_assets (portfolio_id, asset_id, active, mapping_status, mapping_confidence) VALUES (?, ?, 1, 'manual', 1)", (portfolio_id, asset_id))
        source_id = conn.execute(
            "INSERT INTO source_files (asset_id, file_type, original_filename, stored_path, uploaded_at) VALUES (?, 'helioscope', 'h.xlsx', 'h.xlsx', '2026-01-01')",
            (asset_id,),
        ).lastrowid
        conn.execute(
            "INSERT INTO helioscope_expected_production (asset_id, source_file_id, base_year, month, expected_kwh, imported_at) VALUES (?, ?, 2026, 1, 50, '2026-01-01')",
            (asset_id, source_id),
        )
        path = tmp_path / "model.xlsx"
        financial_workbook(path)
        model_id = create_financial_model_preview(conn, upload_dir=tmp_path / "uploads", file_storage=UploadStub(path), asset_id=asset_id, base_year=2026)
        confirm_financial_model_import(conn, model_id=model_id, asset_id=asset_id)
        conn.commit()

        row_2026 = next(row for row in build_portfolio_report_rows(conn, portfolio_id, "2026-01") if row["asset_id"] == asset_id)
        row_2027 = next(row for row in build_portfolio_report_rows(conn, portfolio_id, "2027-01") if row["asset_id"] == asset_id)

        assert row_2026["expected_production_kwh"] == 100
        assert row_2026["expected_production_source"] == "financial_model"
        assert row_2027["expected_production_source"] == "helioscope"
        assert row_2027["expected_production_kwh"] == 50
    finally:
        conn.close()


def test_preview_not_used_in_reports_and_real_values_stay_none(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    try:
        asset_id = add_asset(conn)
        portfolio_id = conn.execute("SELECT id FROM portfolio_groups WHERE name = 'Solcorelios I'").fetchone()["id"]
        conn.execute("INSERT INTO portfolio_assets (portfolio_id, asset_id, active, mapping_status, mapping_confidence) VALUES (?, ?, 1, 'manual', 1)", (portfolio_id, asset_id))
        path = tmp_path / "model.xlsx"
        financial_workbook(path)
        create_financial_model_preview(conn, upload_dir=tmp_path / "uploads", file_storage=UploadStub(path), asset_id=asset_id, base_year=2026)
        conn.commit()

        row = next(row for row in build_portfolio_report_rows(conn, portfolio_id, "2026-01") if row["asset_id"] == asset_id)

        assert row["expected_production_source"] == "none"
        assert row["actual_production_kwh"] is None
        assert row["self_use_kwh"] is None
        assert row["export_kwh"] is None
        assert row["consumption_kwh"] is None
    finally:
        conn.close()


def test_financial_model_routes_block_idor(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    try:
        asset_a = add_asset(conn, name="A")
        asset_b = add_asset(conn, name="B")
        path = tmp_path / "model.xlsx"
        financial_workbook(path)
        model_id = create_financial_model_preview(conn, upload_dir=tmp_path / "uploads", file_storage=UploadStub(path), asset_id=asset_a, base_year=2026)
        conn.commit()
    finally:
        conn.close()
    previous_db = app.config["DATABASE"]
    app.config["DATABASE"] = str(tmp_path / "financial_models.db")
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
        assert client.get(f"/asset/{asset_b}/financial-model/{model_id}/preview").status_code == 404
    finally:
        app.config["DATABASE"] = previous_db
