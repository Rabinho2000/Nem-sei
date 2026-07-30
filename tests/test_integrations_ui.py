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


def test_sigenergy_ui_hides_technical_endpoints_and_manual_system_allowlist(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-operational-ui.db"
    app_module.ensure_database(str(db_path))
    monkeypatch.delenv("SIGENERGY_APP_SECRET", raising=False)
    with get_db(str(db_path)) as conn:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO integration_configs (
                provider, username, password, enabled, created_at, updated_at
            ) VALUES ('Sigenergy', '', '', 0, ?, ?)
            """,
            (now, now),
        )
        conn.commit()

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.get("/integrations")
    finally:
        flask_app.config["DATABASE"] = original_database

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="system_ids"' not in html
    assert "Energy flow endpoint" not in html
    assert "System IDs opcionais" not in html
    assert 'value="refresh_sigenergy_systems"' in html


def test_sigenergy_connection_test_lists_systems_without_energy_flow(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-test-connection.db"
    app_module.ensure_database(str(db_path))
    calls = []

    def fake_check(_conn, provider, **kwargs):
        calls.append((provider, kwargs))
        return {
            "station_count": 2,
            "realtime_count": 0,
            "failed_realtime_count": 0,
        }

    monkeypatch.setattr(app_module, "run_sigenergy_check", fake_check)
    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.post(
            "/integrations",
            data={
                "csrf_token": "token",
                "action": "test_sigenergy_connection",
            },
        )
    finally:
        flask_app.config["DATABASE"] = original_database

    assert response.status_code == 302
    assert calls == [
        (
            app_module.INTEGRATION_PROVIDER_SIGENERGY,
            {"dry_run": True, "include_energy_flow": False},
        )
    ]


def test_sigenergy_onboarding_requires_explicit_remote_confirmation(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-onboarding-confirmation.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        app_module.ensure_integration_seed_data(conn)
        conn.commit()
    calls: list[str] = []

    def fake_onboarding(_conn, _config, system_id, **_kwargs):
        calls.append(system_id)
        return {
            "system_id": system_id,
            "status": "requested",
            "message": "pending",
        }

    monkeypatch.setattr(
        app_module,
        "create_sigenergy_onboarding_request",
        fake_onboarding,
    )
    flask_app, original_database, client = authenticated_client(db_path)
    try:
        rejected = client.post(
            "/integrations",
            data={
                "csrf_token": "token",
                "action": "onboard_sigenergy_system",
                "system_id": "SIG-CONFIRM",
            },
        )
        accepted = client.post(
            "/integrations",
            data={
                "csrf_token": "token",
                "action": "onboard_sigenergy_system",
                "system_id": "SIG-CONFIRM",
                "confirm_remote_access": "1",
            },
        )
    finally:
        flask_app.config["DATABASE"] = original_database

    assert rejected.status_code == 302
    assert accepted.status_code == 302
    assert calls == ["SIG-CONFIRM"]


def test_sigenergy_local_association_can_change_and_remove_in_preview(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-local-association.db"
    app_module.ensure_database(str(db_path))
    monkeypatch.setenv("APP_ENV", "preview")
    now = datetime.now().isoformat(timespec="seconds")
    with get_db(str(db_path)) as conn:
        first_asset = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Asset A')"
            ).lastrowid
        )
        second_asset = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Asset B')"
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO provider_system_inventory (
                provider, external_id, external_name, metadata_json,
                access_status, first_discovered_at, last_discovered_at,
                data_quality, created_at, updated_at
            ) VALUES (
                'Sigenergy', 'TZXRS1780315946', 'Expertcom', '{}',
                'accessible', ?, ?, 'missing', ?, ?
            )
            """,
            (now, now, now, now),
        )
        conn.commit()

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        for asset_id in (first_asset, second_asset):
            response = client.post(
                "/integrations",
                data={
                    "csrf_token": "token",
                    "action": "set_sigenergy_asset_association",
                    "external_id": "TZXRS1780315946",
                    "asset_id": str(asset_id),
                },
            )
            assert response.status_code == 302
            with get_db(str(db_path)) as conn:
                mapping = conn.execute(
                    """
                    SELECT * FROM asset_integrations
                    WHERE provider = 'Sigenergy'
                      AND external_id = 'TZXRS1780315946'
                    """
                ).fetchone()
            assert mapping["asset_id"] == asset_id
            assert mapping["external_name"] == "Expertcom"
            assert mapping["is_primary_energy_source"] == 0

        removed = client.post(
            "/integrations",
            data={
                "csrf_token": "token",
                "action": "set_sigenergy_asset_association",
                "external_id": "TZXRS1780315946",
                "asset_id": "",
            },
        )
        assert removed.status_code == 302
        with get_db(str(db_path)) as conn:
            mapping_count = conn.execute(
                """
                SELECT COUNT(*) FROM asset_integrations
                WHERE provider = 'Sigenergy'
                  AND external_id = 'TZXRS1780315946'
                """
            ).fetchone()[0]
    finally:
        flask_app.config["DATABASE"] = original_database

    assert mapping_count == 0
