from __future__ import annotations

from nemsei.app import create_app


def test_liveness_does_not_require_database_readiness(settings) -> None:
    client = create_app(settings).test_client()
    assert client.get("/healthz").status_code == 200
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json["reason"] == "database schema is not initialized"


def test_readiness_uses_the_injected_check(settings) -> None:
    client = create_app(settings, readiness_check=lambda: (True, "ready")).test_client()
    assert client.get("/readyz").status_code == 200
