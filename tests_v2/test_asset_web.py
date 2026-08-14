from __future__ import annotations

import re

from nemsei.app import create_app
from tests_v2.test_migrations import upgrade


def csrf_token(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_authenticated_asset_crud_never_calls_a_provider(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    client = create_app(settings).test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"
    form = client.get("/assets/new")
    response = client.post(
        "/assets/new",
        data={
            "csrf_token": csrf_token(form),
            "canonical_name": "Central UI",
            "lifecycle_status": "active",
            "timezone": "Europe/Lisbon",
        },
    )
    assert response.status_code == 302
    assert client.get("/assets").status_code == 200
