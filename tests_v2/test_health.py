from __future__ import annotations

from nemsei.app import create_app
from tests_v2.test_migrations import upgrade


def test_liveness_does_not_require_database_readiness(settings) -> None:
    client = create_app(settings).test_client()
    assert client.get("/healthz").status_code == 200
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json["reason"] == "database unavailable"


def test_readiness_uses_the_injected_check(settings) -> None:
    client = create_app(settings, readiness_check=lambda: (True, "ready")).test_client()
    assert client.get("/readyz").status_code == 200


def test_readiness_requires_current_alembic_revision(settings, monkeypatch) -> None:
    app = create_app(settings)
    assert app.test_client().get("/readyz").status_code == 503
    upgrade(settings, monkeypatch)
    assert app.test_client().get("/readyz").status_code == 200
