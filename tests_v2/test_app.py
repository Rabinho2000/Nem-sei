from __future__ import annotations

from nemsei.app import create_app


def test_create_app_does_not_start_background_roles(settings) -> None:
    app = create_app(settings)
    assert "nemsei.scheduler" not in app.extensions
    assert "nemsei.worker" not in app.extensions
