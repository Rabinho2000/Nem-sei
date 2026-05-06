from __future__ import annotations

import re
import os

import pytest

os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["APP_USERNAME"] = "admin"
os.environ["APP_PASSWORD"] = "test-password"

from app import app
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
