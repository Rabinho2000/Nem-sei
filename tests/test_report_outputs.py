from __future__ import annotations

import sqlite3
from pathlib import Path

import app as app_module
import monitoring_board.app_factory as app_factory_module
from openpyxl import load_workbook
from pypdf import PdfReader
from app import ensure_database
from monitoring_board.portfolio_report_repository import get_default_profile
from monitoring_board.portfolio_repository import create_portfolio
from monitoring_board.report_template_repository import (
    archive_template,
    duplicate_template,
    get_default_template,
    latest_template_version,
    list_templates,
    save_template,
    set_default_template,
)
from monitoring_board.reporting.templates import default_template
from monitoring_board.services.portfolio_reporting import prepare_portfolio_report
from monitoring_board.reporting.templates import TemplateSection
from monitoring_board.services.report_rendering import render_individual_excel, render_portfolio_excel, render_portfolio_html, render_portfolio_pdf, render_zip, safe_filename


def connect(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "outputs.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_asset(conn: sqlite3.Connection, name: str = "Output Solar") -> int:
    cursor = conn.execute(
        "INSERT INTO assets (project_name, nif, active_contract, kwp, mounting_date, start_contract) VALUES (?, '501123123', 'yes', '10', '2024-01-01', '2024-01-01')",
        (name,),
    )
    return int(cursor.lastrowid)


def add_portfolio(conn: sqlite3.Connection, asset_id: int) -> int:
    portfolio_id = create_portfolio(conn, name=f"Output Portfolio {asset_id}")
    conn.execute(
        "INSERT INTO portfolio_assets (portfolio_id, asset_id, external_name, active, mapping_status, mapping_confidence, display_order) VALUES (?, ?, 'Output Solar', 1, 'manual', 1, 10)",
        (portfolio_id, asset_id),
    )
    conn.commit()
    return portfolio_id


def test_template_crud_default_version_and_invalid_config(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    templates = list_templates(conn)
    assert {"Individual padrao", "Portfolio executivo"} <= {row["name"] for row in templates}

    template_id = save_template(conn, default_template("Portfolio operacional"), is_default=1)
    duplicate_id = duplicate_template(conn, template_id, "Portfolio operacional copia")
    archive_template(conn, duplicate_id)
    set_default_template(conn, template_id)

    assert latest_template_version(conn, template_id) == 1
    assert get_default_template(conn, "portfolio").id == template_id
    invalid = default_template("Portfolio executivo")
    invalid = invalid.__class__(**{**invalid.__dict__, "name": "", "report_type": "portfolio"})
    try:
        save_template(conn, invalid)
    except ValueError as exc:
        assert str(exc) == "template_name_required"
    else:
        raise AssertionError("expected invalid template")


def test_safe_filename_blocks_traversal_and_reserved_names() -> None:
    assert safe_filename("Cliente Instalação 2026 01", extension="pdf") == "Cliente_Instalacao_2026_01.pdf"
    assert safe_filename("CON", extension="pdf") == "_CON.pdf"
    try:
        safe_filename("../bad", extension="pdf")
    except ValueError as exc:
        assert str(exc) == "unsafe_filename"
    else:
        raise AssertionError("expected unsafe filename")


def test_portfolio_renderers_use_canonical_result(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    portfolio_id = add_portfolio(conn, add_asset(conn))
    profile = get_default_profile(conn, portfolio_id)
    result = prepare_portfolio_report(conn, portfolio_id=portfolio_id, portfolio_name="Output Portfolio", profile=profile, report_month="2026-01")
    template = get_default_template(conn, "portfolio", portfolio_id)

    html = render_portfolio_html(result, template)
    pdf = render_portfolio_pdf(result, template)
    excel = render_portfolio_excel(result, template)
    zipped = render_zip([pdf, excel])

    assert "Output Portfolio" in html
    assert pdf.content.startswith(b"%PDF-")
    assert excel.content.startswith(b"PK")
    assert zipped.content.startswith(b"PK")


def test_report_generation_routes_create_files_and_download(tmp_path: Path) -> None:
    db_path = tmp_path / "routes.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    asset_id = add_asset(conn)
    portfolio_id = add_portfolio(conn, asset_id)
    template_id = next(row["id"] for row in list_templates(conn, "portfolio") if row["name"] == "Portfolio executivo")
    conn.close()

    flask_app = app_module.app
    previous_db = flask_app.config["DATABASE"]
    previous_testing = flask_app.config.get("TESTING")
    flask_app.config["DATABASE"] = str(db_path)
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "token"
    try:
        templates_page = client.get("/report-templates")
        assert templates_page.status_code == 200
        assert b"Templates" in templates_page.data
        preview = client.get(f"/report-generation/preview?portfolio_id={portfolio_id}&template_id={template_id}&report_month=2026-01")
        assert preview.status_code == 200
        assert b"Output Portfolio" in preview.data
        response = client.post(
            "/report-generation",
            data={
                "csrf_token": "token",
                "report_type": "portfolio",
                "template_id": str(template_id),
                "portfolio_id": str(portfolio_id),
                "report_month": "2026-01",
                "period_type": "monthly",
                "formats": ["pdf", "excel", "zip"],
            },
        )
        assert response.status_code in {302, 303}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        file_row = conn.execute("SELECT * FROM report_generated_files WHERE format = 'zip' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert file_row is not None
        download = client.get(f"/report-generation/files/{file_row['id']}")
        assert download.status_code == 200
        assert download.mimetype == "application/zip"
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_batch_partial_counts_metadata_and_auxiliary_zip(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "batch.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    asset_ids = [add_asset(conn, f"Batch {index}") for index in range(3)]
    template_id = next(row["id"] for row in list_templates(conn, "individual") if row["name"] == "Individual padrao")
    conn.close()

    def fake_report(conn, *, asset_id, report_month, electricity_price, sell_price, billing_config, period=None, **kwargs):
        if asset_id == asset_ids[1]:
            return None
        return {
            "asset_id": asset_id,
            "asset": {"id": asset_id, "project_name": f"Batch {asset_id}"},
            "period_label": report_month,
            "period_type": "monthly",
            "period_start": f"{report_month}-01",
            "period_end": f"{report_month}-28",
            "production_kwh": 10,
            "self_use_kwh": 7,
            "export_kwh": 3,
            "consumption_kwh": 9,
            "grid_import_kwh": 2,
            "net_benefit_eur": 1,
        }

    monkeypatch.setattr(app_factory_module, "build_local_customer_production_report", fake_report)
    flask_app = app_module.app
    previous_db = flask_app.config["DATABASE"]
    previous_testing = flask_app.config.get("TESTING")
    flask_app.config["DATABASE"] = str(db_path)
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "token"
    try:
        response = client.post(
            "/report-generation",
            data={
                "csrf_token": "token",
                "report_type": "individual",
                "template_id": str(template_id),
                "asset_ids": [str(item) for item in asset_ids],
                "report_month": "2026-01",
                "formats": ["pdf", "zip"],
            },
        )
        assert response.status_code in {302, 303}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        run = conn.execute("SELECT * FROM report_generation_runs ORDER BY id DESC LIMIT 1").fetchone()
        files = conn.execute("SELECT * FROM report_generated_files WHERE run_id = ? ORDER BY id", (run["id"],)).fetchall()
        conn.close()
        assert run["status"] == "partial"
        assert run["requested_count"] == 3
        assert run["completed_count"] == 2
        assert run["failed_count"] == 1
        assert {row["asset_id"] for row in files if row["status"] == "completed" and row["is_auxiliary"] == 0} == {asset_ids[0], asset_ids[2]}
        assert sum(row["is_auxiliary"] for row in files) == 1
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_portfolio_two_periods_pdf_excel_zip_counts_zip_auxiliary(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio-batch.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    portfolio_id = add_portfolio(conn, add_asset(conn))
    template_id = next(row["id"] for row in list_templates(conn, "portfolio") if row["name"] == "Portfolio executivo")
    conn.close()
    flask_app = app_module.app
    previous_db = flask_app.config["DATABASE"]
    previous_testing = flask_app.config.get("TESTING")
    flask_app.config["DATABASE"] = str(db_path)
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "token"
    try:
        response = client.post(
            "/report-generation",
            data={
                "csrf_token": "token",
                "report_type": "portfolio",
                "template_id": str(template_id),
                "portfolio_id": str(portfolio_id),
                "report_months": "2026-01,2026-02",
                "formats": ["pdf", "excel", "zip"],
            },
        )
        assert response.status_code in {302, 303}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        run = conn.execute("SELECT * FROM report_generation_runs ORDER BY id DESC LIMIT 1").fetchone()
        files = conn.execute("SELECT * FROM report_generated_files WHERE run_id = ?", (run["id"],)).fetchall()
        conn.close()
        assert run["requested_count"] == 4
        assert run["completed_count"] == 4
        assert run["failed_count"] == 0
        assert sum(row["is_auxiliary"] for row in files) == 1
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_invalid_formats_and_limits_are_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "invalid.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    template_id = next(row["id"] for row in list_templates(conn, "portfolio") if row["name"] == "Portfolio executivo")
    conn.close()
    flask_app = app_module.app
    previous_db = flask_app.config["DATABASE"]
    previous_testing = flask_app.config.get("TESTING")
    flask_app.config["DATABASE"] = str(db_path)
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "token"
    try:
        response = client.post("/report-generation", data={"csrf_token": "token", "report_type": "portfolio", "template_id": str(template_id), "formats": ["zip"]})
        assert response.status_code in {302, 303}
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM report_generation_runs").fetchone()[0] == 0
        conn.close()
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_individual_excel_reconciles_with_canonical_report(tmp_path: Path) -> None:
    template = default_template("Individual padrao")
    report = {
        "asset": {"id": 1, "project_name": "Excel Individual"},
        "period_label": "Janeiro 2026",
        "period_type": "monthly",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "production_kwh": 100,
        "self_use_kwh": 70,
        "export_kwh": 30,
        "consumption_kwh": 90,
        "grid_import_kwh": 20,
        "solcor_payment_eur": 7,
        "fixed_monthly_fee_eur": 0,
        "net_benefit_eur": 10,
    }
    rendered = render_individual_excel(report, template)
    path = tmp_path / rendered.filename
    path.write_bytes(rendered.content)
    workbook = load_workbook(path)
    values = {row[0].value: row[1].value for row in workbook["Energia"].iter_rows(min_row=2)}

    assert workbook.sheetnames == ["Resumo", "Energia", "Financeiro", "Qualidade dos dados", "Metadados"]
    assert values["production_kwh"] == report["production_kwh"]
    assert values["self_use_kwh"] == report["self_use_kwh"]


def test_section_order_is_shared_by_preview_and_pdf(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    portfolio_id = add_portfolio(conn, add_asset(conn))
    result = prepare_portfolio_report(conn, portfolio_id=portfolio_id, portfolio_name="Order Portfolio", profile=get_default_profile(conn, portfolio_id), report_month="2026-01")
    template = default_template("Portfolio executivo")
    template = template.__class__(
        **{
            **template.__dict__,
            "sections": (
                TemplateSection("warnings", "Warnings First", True, 10),
                TemplateSection("kpis", "KPIs Second", True, 20),
                TemplateSection("installations_table", "Table Third", True, 30),
            ),
        }
    )
    html = render_portfolio_html(result, template)
    pdf = render_portfolio_pdf(result, template)
    pdf_path = tmp_path / "order.pdf"
    pdf_path.write_bytes(pdf.content)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)

    assert html.index("Warnings First") < html.index("KPIs Second") < html.index("Table Third")
    assert text.index("Warnings First") < text.index("KPIs Second") < text.index("Table Third")


def test_portfolio_pdf_includes_more_than_ten_columns(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    portfolio_id = add_portfolio(conn, add_asset(conn))
    result = prepare_portfolio_report(conn, portfolio_id=portfolio_id, portfolio_name="Wide Portfolio", profile=get_default_profile(conn, portfolio_id), report_month="2026-01")
    template = default_template("Portfolio operacional")
    pdf = render_portfolio_pdf(result, template)
    pdf_path = tmp_path / "wide.pdf"
    pdf_path.write_bytes(pdf.content)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)

    assert len(result.columns) > 10
    assert "Beneficio liquido" in text
    assert "Warnings" in text
