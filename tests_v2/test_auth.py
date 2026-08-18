from __future__ import annotations

import re

from nemsei.app import create_app
from tests_v2.test_migrations import upgrade


def csrf_token(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_login_and_csrf_protect_authenticated_routes(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    client = create_app(settings).test_client()
    assert client.get("/").status_code == 302
    login = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "correct-password", "csrf_token": csrf_token(login)},
    )
    assert response.status_code == 302
    assert client.get("/").status_code == 200


def test_login_rejects_missing_csrf_token(settings) -> None:
    client = create_app(settings).test_client()
    assert client.post("/login", data={"username": "admin", "password": "correct-password"}).status_code == 400
