from __future__ import annotations

import re
import os
from io import BytesIO

import pytest

os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["APP_USERNAME"] = "admin"
os.environ["APP_PASSWORD"] = "test-password"

from app import app, ensure_database
from monitoring_board.db import get_db, query_scalar
from monitoring_board.runtime import DEFAULT_MAX_UPLOAD_BYTES
from monitoring_board.routes.auth import safe_local_next_url
from monitoring_board.security import flask_secret_key


def csrf_from(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def test_dashboard_requires_login() -> None:
    response = app.test_client().get("/")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_and_csrf_flow() -> None:
    client = app.test_client()
    login_page = client.get("/login")
    token = csrf_from(login_page)

    login_response = client.post(
        "/login",
        data={"csrf_token": token, "username": "admin", "password": "test-password"},
    )

    assert login_response.status_code == 302
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 400


def test_session_cookie_defaults_are_hardened() -> None:
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["MAX_CONTENT_LENGTH"] > 0
    assert DEFAULT_MAX_UPLOAD_BYTES >= 50 * 1024 * 1024


def test_login_rejects_external_next_redirect() -> None:
    client = app.test_client()
    login_page = client.get("/login?next=//evil.example/path")
    token = csrf_from(login_page)

    login_response = client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "admin",
            "password": "test-password",
            "next": "//evil.example/path",
        },
    )

    assert login_response.status_code == 302
    assert login_response.headers["Location"] == "/"


def test_safe_local_next_url_allows_local_paths() -> None:
    with app.test_request_context():
        assert safe_local_next_url("/assets?search=x") == "/assets?search=x"
        assert safe_local_next_url("https://evil.example") == "/"
        assert safe_local_next_url("//evil.example/path") == "/"


def test_flask_secret_key_rejects_known_insecure_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ["", "change-me", "monitoring-board-local-secret"]:
        monkeypatch.setenv("FLASK_SECRET_KEY", value)
        with pytest.raises(RuntimeError):
            flask_secret_key()


def test_invalid_post_has_friendly_error_page() -> None:
    response = app.test_client().post("/login", data={})

    assert response.status_code == 400
    assert b"Pedido invalido" in response.data


def test_contract_upload_rejects_non_pdf_content(tmp_path) -> None:
    db_path = tmp_path / "security.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        cursor = conn.execute("INSERT INTO assets (project_name) VALUES (?)", ("Central A",))
        asset_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()

    previous_db = app.config["DATABASE"]
    app.config["DATABASE"] = str(db_path)
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
            sess["csrf_token"] = "token"
        response = client.post(
            f"/asset/{asset_id}/contract",
            data={
                "csrf_token": "token",
                "contract_pdf": (BytesIO(b"not a pdf"), "contract.pdf"),
            },
            content_type="multipart/form-data",
        )
    finally:
        app.config["DATABASE"] = previous_db

    conn = get_db(str(db_path))
    try:
        assert response.status_code == 302
        assert query_scalar(conn, "SELECT COUNT(*) FROM om_contracts") == 0
    finally:
        conn.close()


def test_asset_detail_renders_financial_model_summary(tmp_path) -> None:
    db_path = tmp_path / "asset_financial.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        cursor = conn.execute("INSERT INTO assets (project_name) VALUES (?)", ("Central A",))
        asset_id = int(cursor.lastrowid)
        source_file_id = int(conn.execute(
            """
            INSERT INTO source_files (
                asset_id, file_type, original_filename, stored_path, uploaded_at, sha256
            ) VALUES (?, 'financial_model', 'financial.xlsm', 'uploads/financial_models/1/financial.xlsm', '2026-07-10T10:00:00', 'abc')
            """,
            (asset_id,),
        ).lastrowid)
        model_id = int(conn.execute(
            """
            INSERT INTO financial_models (
                source_file_id, asset_id, base_year, version, status, active,
                detected_name, detected_kwp, parser_name, parser_version, file_sha256,
                warnings_json, validation_json, confirmed_at, created_at, updated_at
            ) VALUES (?, ?, 2026, 1, 'confirmed', 1, 'Central A', 10, 'test', '1', 'abc', '[]', '{}',
                      '2026-07-10T10:00:00', '2026-07-10T10:00:00', '2026-07-10T10:00:00')
            """,
            (source_file_id, asset_id),
        ).lastrowid)
        conn.execute(
            """
            INSERT INTO financial_model_monthly (
                financial_model_id, asset_id, base_year, month, expected_production_kwh
            ) VALUES (?, ?, 2026, 1, 123.45)
            """,
            (model_id, asset_id),
        )
        conn.commit()
    finally:
        conn.close()

    previous_db = app.config["DATABASE"]
    app.config["DATABASE"] = str(db_path)
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
        response = client.get(f"/asset/{asset_id}")
    finally:
        app.config["DATABASE"] = previous_db

    assert response.status_code == 200
    assert b"Modelo financeiro" in response.data
    assert b"financial.xlsm" in response.data
    assert b"123.45" in response.data


def test_contract_open_blocks_paths_outside_contract_directory(tmp_path) -> None:
    db_path = tmp_path / "security.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        cursor = conn.execute("INSERT INTO assets (project_name) VALUES (?)", ("Central A",))
        asset_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO om_contracts (asset_id, pdf_path, original_filename, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (asset_id, "../.env", ".env", "2026-05-06T10:00:00", "2026-05-06T10:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    previous_db = app.config["DATABASE"]
    app.config["DATABASE"] = str(db_path)
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
        response = client.get(f"/asset/{asset_id}/contract/open")
    finally:
        app.config["DATABASE"] = previous_db

    assert response.status_code == 404
