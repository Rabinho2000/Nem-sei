from __future__ import annotations

from datetime import datetime

import app as app_module
from monitoring_board.db import get_db


def authenticated_client(db_path):
    flask_app = app_module.app
    original_database = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "admin"
        sess["csrf_token"] = "token"
    return flask_app, original_database, client


def test_integrations_page_does_not_show_configured_secrets(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "integrations-ui.db"
    app_module.ensure_database(str(db_path))
    monkeypatch.delenv("FUSIONSOLAR_PASSWORD", raising=False)
    monkeypatch.delenv("SIGENERGY_APP_SECRET", raising=False)
    secret = "super-secret-do-not-leak"
    sig_secret = "sig-secret-do-not-leak"
    now = datetime.now().isoformat(timespec="seconds")
    with get_db(str(db_path)) as conn:
        conn.execute(
            """
            UPDATE integration_configs
            SET username = 'fusion-user', password = ?, base_url = 'https://fusion.test',
                enabled = 1, updated_at = ?
            WHERE provider = 'FusionSolar'
            """,
            (secret, now),
        )
        conn.execute(
            """
            UPDATE integration_configs
            SET username = 'sig-user', password = ?, base_url = 'https://sig.test',
                enabled = 1, updated_at = ?
            WHERE provider = 'Sigenergy'
            """,
            (sig_secret, now),
        )
        conn.commit()

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.get("/integrations")
    finally:
        flask_app.config["DATABASE"] = original_database

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert secret not in html
    assert sig_secret not in html


def test_integrations_ui_does_not_force_hidden_auto_sync_and_has_daily_fields(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "integrations-ui-fields.db"
    app_module.ensure_database(str(db_path))
    monkeypatch.delenv("FUSIONSOLAR_PASSWORD", raising=False)
    monkeypatch.delenv("SIGENERGY_APP_SECRET", raising=False)

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.get("/integrations")
    finally:
        flask_app.config["DATABASE"] = original_database

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'type="hidden" name="auto_sync_enabled"' not in html
    assert 'name="production_sync_enabled"' in html
    assert 'name="diagnostics_sync_enabled"' in html
    assert 'name="production_sync_time"' in html
    assert 'name="diagnostics_sync_time"' in html
