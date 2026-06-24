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
from monitoring_board.services.invoice_extraction import extract_candidates_from_text, extract_invoice_file


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
    client = None
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


def make_xlsx_bytes(text: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for line in text.splitlines():
        sheet.append([line])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_pdf_bytes(text: str = "Fatura") -> bytes:
    buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(buffer)
    pdf_canvas.drawString(40, 760, text.replace("\n", " "))
    pdf_canvas.save()
    return buffer.getvalue()


def post_invoice(client, asset_id: int, payload: bytes, filename: str):
    return client.post(
        "/portfolios/upload-invoice",
        data={"csrf_token": "token", "asset_id": str(asset_id), "portfolio_id": "1", "file": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )


def invoice_form(action: str, **overrides: str) -> dict[str, str]:
    data = {
        "csrf_token": "token",
        "action": action,
        "invoice_number": "FT 2026/1",
        "issue_date": "01/02/2026",
        "customer_nif": "123456789",
        "billing_period_start": "01/01/2026",
        "billing_period_end": "31/01/2026",
        "currency": "EUR",
        "tariff_type_candidate": "simple",
        "simple_price_eur_kwh": "0.20123",
    }
    data.update(overrides)
    return data


def test_invoice_domain_normalizes_values_and_validates() -> None:
    assert invoices.normalize_decimal("1.234,56 EUR") == Decimal("1234.56")
    assert invoices.normalize_decimal("1,234.56") == Decimal("1234.56")
    assert invoices.normalize_decimal("1234,56") == Decimal("1234.56")
    assert invoices.normalize_decimal("1234.56") == Decimal("1234.56")
    assert invoices.normalize_decimal("1 234,56") == Decimal("1234.56")
    assert invoices.normalize_decimal("0,21450") == Decimal("0.21450")
    assert invoices.normalize_decimal("0.21450") == Decimal("0.21450")
    for value in ("NaN", "Infinity", "12abc", "1.234.56", "1,234,56"):
        try:
            invoices.normalize_decimal(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid decimal: {value}")
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
    assert invoices.warnings_require_override(("missing_asset_nif", "missing_invoice_number")) is False
    assert invoices.warnings_require_override(("invalid_customer_nif",)) is True
    assert invoices.infer_tariff_type({"cheia_price_eur_kwh": "0.2", "vazio_price_eur_kwh": "0.1"}) == app_module.TariffType.BI_HOURLY


def test_invoice_nif_extraction_variants_and_explicit_labels() -> None:
    cases = (
        ("NIF: 501234567", "501234567"),
        ("NIF: PT501234567", "501234567"),
        ("NIF: PT 501 234 567", "501234567"),
        ("VAT: 501-234-567", "501234567"),
    )
    for text, expected in cases:
        values = {candidate.field_name: candidate.value for candidate in extract_candidates_from_text(text, source="txt")}
        assert values["customer_nif"] == expected

    labelled = """
    Fornecedor NIF: PT501964843
    Cliente NIF: PT 123 456 789
    """
    values = {candidate.field_name: candidate.value for candidate in extract_candidates_from_text(labelled, source="txt")}
    assert values["supplier_nif"] == "501964843"
    assert values["customer_nif"] == "123456789"

    one_nif = {candidate.field_name: candidate.value for candidate in extract_candidates_from_text("NIF: 123456789", source="txt")}
    assert one_nif["customer_nif"] == "123456789"
    too_long = {candidate.field_name: candidate.value for candidate in extract_candidates_from_text("NIF: 1234567890", source="txt")}
    assert "customer_nif" not in too_long
    invalid = invoices.validate_invoice_values({"customer_nif": "501234567"})
    assert "invalid_customer_nif" in invalid.warnings


def test_invoice_extraction_reads_text_csv_excel_pdf_and_scanned_pdf(tmp_path: Path) -> None:
    txt = tmp_path / "invoice.txt"
    txt.write_text(invoice_text(), encoding="utf-8")
    csv_path = tmp_path / "invoice.csv"
    csv_path.write_text(invoice_text(), encoding="utf-8")
    xlsx = tmp_path / "invoice.xlsx"
    xlsx.write_bytes(make_xlsx_bytes(invoice_text(price="0.30123")))
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(make_pdf_bytes(invoice_text(price="0.40123")))
    scanned = tmp_path / "scanned.pdf"
    scanned.write_bytes(make_pdf_bytes(""))

    assert extract_invoice_file(txt).tariff_candidate.tariff_type == app_module.TariffType.SIMPLE
    assert extract_invoice_file(csv_path).candidates
    assert extract_invoice_file(xlsx).tariff_candidate.simple_price_eur_kwh == Decimal("0.30123")
    assert extract_invoice_file(pdf).tariff_candidate.simple_price_eur_kwh == Decimal("0.40123")
    assert "scanned_pdf_requires_manual_review" in extract_invoice_file(scanned).warnings


def test_invoice_content_validation_blocks_renamed_binary_files(tmp_path: Path) -> None:
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(make_pdf_bytes("Fatura"))
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"MZ executable")
    valid_xlsx = tmp_path / "valid.xlsx"
    valid_xlsx.write_bytes(make_xlsx_bytes(invoice_text()))
    fake_xlsx = tmp_path / "fake.xlsx"
    fake_xlsx.write_bytes(b"MZ executable")
    valid_txt = tmp_path / "valid.txt"
    valid_txt.write_text(invoice_text(), encoding="utf-8")
    binary_txt = tmp_path / "binary.txt"
    binary_txt.write_bytes(b"NIF\x00MZ")

    assert extract_invoice_file(valid_pdf).errors == ()
    assert "invalid_pdf_signature" in extract_invoice_file(fake_pdf).errors
    assert extract_invoice_file(valid_xlsx).errors == ()
    assert "invalid_office_zip" in extract_invoice_file(fake_xlsx).errors
    assert extract_invoice_file(valid_txt).errors == ()
    assert "binary_text_invoice" in extract_invoice_file(binary_txt).errors


def test_invoice_price_extraction_prefers_unit_price_over_energy_and_total(tmp_path: Path) -> None:
    text = """
    Ponta 1 234 kWh Preco unitario 0,21450 EUR/kWh Total 264,75 EUR
    Cheia 456 kWh 0,18400 EUR/kWh Total 83,90 EUR
    Vazio 789 kWh 0,09900 EUR/kWh Total 78,11 EUR
    Super vazio 100 kWh 0,08100 EUR/kWh Total 8,10 EUR
    """
    result = extract_candidates_from_text(text, source="txt")
    values = {candidate.field_name: candidate.value for candidate in result}
    assert values["ponta_price_eur_kwh"] == "0.21450"
    assert values["cheia_price_eur_kwh"] == "0.18400"
    assert values["vazio_price_eur_kwh"] == "0.09900"
    assert values["super_vazio_price_eur_kwh"] == "0.08100"

    ambiguous = tmp_path / "ambiguous.txt"
    ambiguous.write_text("Ponta 1234 kWh 264,75 EUR", encoding="utf-8")
    extracted = extract_invoice_file(ambiguous)
    assert extracted.tariff_candidate.ponta_price_eur_kwh is None
    assert "ambiguous_invoice_price" in extracted.warnings


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
        review = client.post(f"/invoices/{doc_id}/review", data=invoice_form("confirm_invoice"))
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


def test_invoice_upload_blocks_formats_duplicates_and_fake_content(tmp_path: Path) -> None:
    db_path = tmp_path / "invoices.db"
    conn = connect(tmp_path)
    asset_a = add_asset(conn, nif="123456789")
    asset_b = add_asset(conn, nif="508456789")
    conn.close()
    flask_app, previous_db, previous_testing, client = csrf_client(db_path)
    try:
        assert post_invoice(client, asset_a, b"x", "bad.exe").status_code in {302, 303}
        assert post_invoice(client, asset_a, b"MZ executable", "fake.pdf").status_code in {302, 303}
        payload = invoice_text().encode("utf-8")
        for asset_id in (asset_a, asset_a, asset_b):
            post_invoice(client, asset_id, payload, "invoice.txt")
        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            docs = check.execute("SELECT * FROM invoice_documents ORDER BY asset_id").fetchall()
            assert len(docs) == 2
            warnings = [json.loads(row["warnings_json"] or "[]") for row in docs]
            assert any("possible_duplicate_invoice" in item for item in warnings)
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_invoice_review_warning_classes_and_explicit_override(tmp_path: Path) -> None:
    db_path = tmp_path / "invoices.db"
    conn = connect(tmp_path)
    no_nif_asset = add_asset(conn, nif="")
    mismatch_asset = add_asset(conn, nif="123456789")
    conn.close()
    flask_app, previous_db, previous_testing, client = csrf_client(db_path)
    try:
        post_invoice(client, no_nif_asset, invoice_text().encode("utf-8"), "invoice.txt")
        post_invoice(client, mismatch_asset, invoice_text().encode("utf-8"), "invoice-2.txt")
        with sqlite3.connect(db_path) as check:
            docs = check.execute("SELECT id, asset_id FROM invoice_documents ORDER BY id").fetchall()
            no_nif_doc = next(row[0] for row in docs if row[1] == no_nif_asset)
            mismatch_doc = next(row[0] for row in docs if row[1] == mismatch_asset)

        client.post(f"/invoices/{no_nif_doc}/review", data=invoice_form("confirm_invoice"))
        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            invoice = check.execute("SELECT * FROM invoice_documents WHERE id = ?", (no_nif_doc,)).fetchone()
            assert invoice["status"] == "confirmed"
            assert "missing_asset_nif" in json.loads(invoice["warnings_json"])

        client.post(f"/invoices/{mismatch_doc}/review", data=invoice_form("confirm_invoice", customer_nif="508456789"))
        with sqlite3.connect(db_path) as check:
            assert check.execute("SELECT status FROM invoice_documents WHERE id = ?", (mismatch_doc,)).fetchone()[0] != "confirmed"

        client.post(f"/invoices/{mismatch_doc}/review", data=invoice_form("confirm_with_warnings", customer_nif="508456789"))
        with sqlite3.connect(db_path) as check:
            check.row_factory = sqlite3.Row
            invoice = check.execute("SELECT * FROM invoice_documents WHERE id = ?", (mismatch_doc,)).fetchone()
            assert invoice["status"] == "confirmed"
            assert "customer_nif_mismatch" in json.loads(invoice["warnings_json"])
    finally:
        flask_app.config["DATABASE"] = previous_db
        flask_app.config["TESTING"] = previous_testing


def test_invoice_upload_repository_failure_cleans_files_and_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "invoices.db"
    upload_dir = tmp_path / "uploads"
    conn = connect(tmp_path)
    asset_id = add_asset(conn)
    conn.close()
    flask_app, previous_db, previous_testing, client = csrf_client(db_path)
    monkeypatch.setattr(app_module, "UPLOAD_DIR", upload_dir)

    def fail_create_source_file(*args, **kwargs):
        raise RuntimeError("repository failure")

    monkeypatch.setattr(app_module, "create_source_file_record", fail_create_source_file)
    try:
        response = post_invoice(client, asset_id, invoice_text().encode("utf-8"), "invoice.txt")
        assert response.status_code in {302, 303}
        assert not [path for path in upload_dir.rglob("*") if path.is_file()]
        with sqlite3.connect(db_path) as check:
            assert check.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0
            assert check.execute("SELECT COUNT(*) FROM invoice_documents").fetchone()[0] == 0
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
        post_invoice(client, asset_id, invoice_text(nif="508456789").encode("utf-8"), "invoice.txt")
        with sqlite3.connect(db_path) as check:
            doc_id = check.execute("SELECT id FROM invoice_documents").fetchone()[0]
        response = client.post(
            f"/invoices/{doc_id}/review",
            data=invoice_form(
                "confirm_invoice",
                customer_nif="508456789",
                billing_period_start="31/01/2026",
                billing_period_end="01/01/2026",
                simple_price_eur_kwh="-1",
            ),
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
