from __future__ import annotations

import app as app_module
import pytest

from monitoring_board.preview_safety import (
    PREVIEW_DISABLED_MESSAGE,
    ExternalActionDisabled,
)
from monitoring_board.services.telegram_service import send_telegram_message


def authenticated_client(db_path):
    app_module.ensure_database(str(db_path))
    flask_app = app_module.app
    original_database = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "preview-admin"
        session["csrf_token"] = "preview-csrf"
    return flask_app, original_database, client


def test_scheduler_disabled_never_constructs_apscheduler(monkeypatch) -> None:
    original_scheduler = app_module.SCHEDULER
    app_module.SCHEDULER = None
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("PREVIEW_BANNER", "true")

    def unexpected_scheduler(*_args, **_kwargs):
        raise AssertionError("APScheduler must not be constructed in preview")

    monkeypatch.setattr(app_module, "BackgroundScheduler", unexpected_scheduler)
    try:
        app_module.start_integration_scheduler(app_module.app)
        assert app_module.SCHEDULER is None
    finally:
        app_module.SCHEDULER = original_scheduler


def test_preview_banner_is_conditional() -> None:
    flask_app = app_module.app
    original = flask_app.config["PREVIEW_BANNER"]
    try:
        flask_app.config["PREVIEW_BANNER"] = True
        preview_response = flask_app.test_client().get("/login")
        assert "PREVIEW / NÃO PRODUÇÃO".encode() in preview_response.data

        flask_app.config["PREVIEW_BANNER"] = False
        production_response = flask_app.test_client().get("/login")
        assert "PREVIEW / NÃO PRODUÇÃO".encode() not in production_response.data
    finally:
        flask_app.config["PREVIEW_BANNER"] = original


def test_external_client_is_blocked_before_network(monkeypatch) -> None:
    monkeypatch.setenv("PREVIEW_BANNER", "true")
    monkeypatch.setenv("EXTERNAL_ACTIONS_ENABLED", "true")

    with pytest.raises(ExternalActionDisabled, match="PREVIEW"):
        send_telegram_message("must not leave the process")


def test_manual_integration_action_reports_preview_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "preview")
    flask_app, original_database, client = authenticated_client(
        tmp_path / "preview-actions.db"
    )
    try:
        response = client.post(
            "/integrations",
            data={
                "csrf_token": "preview-csrf",
                "action": "test_fusionsolar_connection",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert PREVIEW_DISABLED_MESSAGE.encode() in response.data
    finally:
        flask_app.config["DATABASE"] = original_database
