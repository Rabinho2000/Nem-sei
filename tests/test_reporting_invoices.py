from __future__ import annotations

import io
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from reportlab.pdfgen import canvas

import app as app_module
from app import ensure_database
from monitoring_board.reporting import invoices
from monitoring_board.services.invoice_extraction import extract_invoice_file


def connect(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "invoices.db"
    ensure_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_asset(conn: sqlite3.Connection, *, nif: str = "123456789") -> int:
    cursor = conn.execute("INSERT INTO assets (project_name, nif, active_contract, kwp) VALUES ('Invoice Asset', ?, 'yes', '10')", (nif,))
    asset_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled) VALUES (?, 'FusionSolar', ?, ?, 1)",
        (asset_id, f"S{asset_id}", f"S{asset_id}"),
    )
    conn.commit()
    return asset_id


def csrf_client(db_path: Path):
    flask_app = app_module.app
    previous_db = flask_app.config["DATABASE"]
    previous_testing = flask_app.config.get("TESTING")
    flask_app.config["DATABASE"] = str(db_path)
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "token"
    return flask_app, previous_db, previous_testing, client


def invoice_text(*, price: str = "0,20123", nif: str = "123456789") -> str:
    return f"""
    Fatura FT 2026/1
    NIF: 501964843
    Cliente NIF: {nif}
    Data de emissao: 01/02/2026
    Periodo de faturacao: 01/01/2026 a 31/01/2026
    Total 123,45 EUR
    Energia ativa 100 kWh
    Preco unitario {price} EUR/kWh
    """


def test_invoice_domain_normalizes_values_and_validates() -> None:
    assert invoices.normalize_decimal("1.234,56 €") == Decimal("1234.56")
    assert invoices.normalize_decimal("1234.56") == Decimal("1234.56")
    assert invoices.normalize_date("31/01/2026") == date(2026, 1, 31)
    assert invoices.normalize_nif("PT 123-456-789") == "123456789"
    assert invoices.is_valid_portuguese_nif("123456789") is True
    assert invoices.is_valid_portuguese_nif("501234567") is False
    valid = invoices.validate_invoice_values({"billing_period_start": "01/01/2026", "billing_period_end": "31/01/2026", "customer_nif": "123456789"})
    inverted = invoices.validate_invoice_values({"billing_period_start": "31/01/2026", "billing_period_end": "01/01/2026", "simple_price_eur_kwh": "-1"})
    nonfinite = invoices.validate_invoice_values({"simple_price_eur_kwh": "NaN"})

    assert valid.valid is True
    assert "invalid_billing_period" in inverted.errors
    assert "negative_simple_price_eur_kwh" in inverted.errors
    assert "invalid_simple_price_eur_kwh" in nonfinite.errors
    assert invoices.infer_tariff_type({"cheia_price_eur_kwh": "0.2", "vazio_price_eur_kwh": "0.1"}) == app_module.TariffType.BI_HOURLY


def test_invoice_extraction_reads_text_csv_excel_pdf_and_scanned_pdf(tmp_path: Path) -> None:
    txt = tmp_path / "invoice.txt"
    txt.write_text(invoice_text(), encoding="utf-8")
    csv_path = tmp_path / "invoice.csv"
    csv_path.write_text(invoice_text().replace("\n", "\n"), encoding="utf-8")
    xlsx = tmp_path / "invoice.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    for line in invoice_text(price="0.30123").splitlines():
        sheet.append([line])
    workbook.save(xlsx)
    pdf = tmp_path / "invoice.pdf"
    buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(buffer)
    pdf_canvas.drawString(40, 760, invoice_text(price="0.40123").replace("\n", " "))
    pdf_canvas.save()
    pdf.write_bytes(buffer.getvalue())
    scanned = tmp_path / "scanned.pdf"
    blank = io.BytesIO()
    blank_canvas = canvas.Canvas(blank)
    blank_canvas.showPage()
    blank_canvas.save()
    scanned.write_bytes(blank.getvalue())

    assert extract_invoice_file(txt).tariff_candidate.tariff_type == app_module.TariffType.SIMPLE
    assert extract_invoice_file(csv_path).candidates
    assert extract_invoice_file(xlsx).tariff_candidate.simple_price_eur_kwh == Decimal("0.30123")
    assert extract_invoice_file(pdf).tariff_candidate.simple_price_eur_kwh == Decimal("0.40123")
    assert "scanned_pdf_requires_manual_review" in extract_invoice_file(scanned).warnings


def test_invoice_upload_extract_review_confirm_and_apply_tariff_routes(tmp_path: Path) -> None:
    db_path = tmp_path / "invoices.db"
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    conn.close()
    flask_app, previous_db, previous_testing, client = csrf_client(db_path)
    try:
        response = client.post(
            "/portfolios/upload-invoice",
            data={
                "csrf_token": "token",
                "asset_id": str(asset_id),
                "portfolio_id": "1",
                "report_month": "2026-01",
                "file": (io.BytesIO(invoice_text().encode("utf-8")), "../evil.txt"),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code in {302, 303}
        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            doc = check.execute("SELECT * FROM invoice_documents").fetchone()
            source = check.execute("SELECT * FROM source_files").fetchone()
            assert doc is not None
            assert source["sha256"]
            assert ".." not in source["stored_path"]
            doc_id = int(doc["id"])

        extract_response = client.post(f"/invoices/{doc_id}/extract", data={"csrf_token": "token"})
        assert extract_response.status_code in {302, 303}
        review = client.post(
            f"/invoices/{doc_id}/review",
            data={
                "csrf_token": "token",
                "action": "confirm_invoice",
                "invoice_number": "FT 2026/1",
                "issue_date": "01/02/2026",
                "customer_nif": "123456789",
                "billing_period_start": "01/01/2026",
                "billing_period_end": "31/01/2026",
                "currency": "EUR",
                "tariff_type_candidate": "simple",
                "simple_price_eur_kwh": "0.20123",
            },
        )
        assert review.status_code in {302, 303}
        apply = client.post(f"/invoices/{doc_id}/review", data={"csrf_token": "token", "action": "use_invoice_tariff"})
        assert apply.status_code in {302, 303}
        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            invoice = check.execute("SELECT * FROM invoice_documents WHERE id = ?", (doc_id,)).fetchone()
            tariff = check.execute("SELECT * FROM asset_tariffs WHERE invoice_file_id = ?", (invoice["source_file_id"],)).fetchone()
            assert invoice["status"] == "confirmed"
            assert tariff["simple_price_eur_kwh"] == 0.20123
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_invoice_upload_blocks_formats_and_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "invoices.db"
    conn = connect(tmp_path)
    asset_a = add_asset(conn, nif="123456789")
    asset_b = add_asset(conn, nif="508456789")
    conn.close()
    flask_app, previous_db, previous_testing, client = csrf_client(db_path)
    try:
        blocked = client.post(
            "/portfolios/upload-invoice",
            data={"csrf_token": "token", "asset_id": str(asset_a), "portfolio_id": "1", "file": (io.BytesIO(b"x"), "bad.exe")},
            content_type="multipart/form-data",
        )
        assert blocked.status_code in {302, 303}
        payload = invoice_text().encode("utf-8")
        for asset_id in (asset_a, asset_a, asset_b):
            client.post(
                "/portfolios/upload-invoice",
                data={"csrf_token": "token", "asset_id": str(asset_id), "portfolio_id": "1", "file": (io.BytesIO(payload), "invoice.txt")},
                content_type="multipart/form-data",
            )
        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            docs = check.execute("SELECT * FROM invoice_documents ORDER BY asset_id").fetchall()
            assert len(docs) == 2
            warnings = [json.loads(row["warnings_json"] or "[]") for row in docs]
            assert any("possible_duplicate_invoice" in item for item in warnings)
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_invoice_review_rejects_nif_mismatch_and_invalid_values(tmp_path: Path) -> None:
    db_path = tmp_path / "invoices.db"
    conn = connect(tmp_path)
    asset_id = add_asset(conn, nif="123456789")
    conn.close()
    flask_app, previous_db, previous_testing, client = csrf_client(db_path)
    try:
        client.post(
            "/portfolios/upload-invoice",
            data={"csrf_token": "token", "asset_id": str(asset_id), "portfolio_id": "1", "file": (io.BytesIO(invoice_text(nif="508456789").encode("utf-8")), "invoice.txt")},
            content_type="multipart/form-data",
        )
        with sqlite3.connect(db_path) as check:
            doc_id = check.execute("SELECT id FROM invoice_documents").fetchone()[0]
        response = client.post(
            f"/invoices/{doc_id}/review",
            data={
                "csrf_token": "token",
                "action": "confirm_invoice",
                "customer_nif": "508456789",
                "billing_period_start": "31/01/2026",
                "billing_period_end": "01/01/2026",
                "simple_price_eur_kwh": "-1",
            },
        )
        assert response.status_code in {302, 303}
        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            invoice = check.execute("SELECT * FROM invoice_documents WHERE id = ?", (doc_id,)).fetchone()
            assert invoice["status"] != "confirmed"
        client.post(f"/invoices/{doc_id}/review", data={"csrf_token": "token", "action": "reject_invoice"})
        with sqlite3.connect(db_path) as check:
            assert check.execute("SELECT status FROM invoice_documents WHERE id = ?", (doc_id,)).fetchone()[0] == "rejected"
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing
