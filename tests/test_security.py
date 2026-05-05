from __future__ import annotations

import re
import os

os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["APP_USERNAME"] = "admin"
os.environ["APP_PASSWORD"] = "test-password"

from app import app


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


def test_invalid_post_has_friendly_error_page() -> None:
    response = app.test_client().post("/login", data={})

    assert response.status_code == 400
    assert b"Pedido invalido" in response.data
