from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from werkzeug.datastructures import MultiDict

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.services.sigenergy_errors import SigenergyApiError


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


def normalized(provider: str, external_id: str, name: str) -> dict:
    return {
        "provider": provider,
        "source_label": f"{provider} API",
        "external_id": external_id,
        "external_name": name,
        "project_name": name,
        "company_name": "API Owner",
        "address": "API Street",
        "location": "Lisboa",
        "country": "PT",
        "timezone": "Europe/Lisbon",
        "kwp": 50.0,
        "kwac": 45.0,
        "commissioning_date": "2024-01-15",
        "operational_status": "Operacional",
        "inverter_count": 1,
        "devices": [],
        "pv_power_kw": 12.5,
        "load_power_kw": 3.0,
        "grid_power_kw": -9.5,
        "battery_power_kw": 1.2 if provider == "Sigenergy" else None,
        "battery_soc_pct": 77 if provider == "Sigenergy" else None,
        "battery_capacity_kwh": 10 if provider == "Sigenergy" else None,
        "ev_power_kw": None,
        "heat_pump_power_kw": None,
        "alarm_count": 0,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def preview_import(
    conn,
    *,
    provider: str,
    external_id: str,
    name: str,
    mode: str = "create",
    target_asset_id: int | None = None,
    raw_payload: dict | None = None,
) -> int:
    import_id = app_module.upsert_installation_import_preview(
        conn,
        provider=provider,
        external_id=external_id,
        external_name=name,
        mode=mode,
        target_asset_id=target_asset_id,
        status="preview",
        access_status="available",
        normalized=normalized(provider, external_id, name),
        raw_payload=raw_payload or {},
        created_by="test",
    )
    conn.commit()
    return import_id


def confirm_form(name: str, *, target_asset_id: int | None = None, apply_fields: list[str] | None = None):
    values = [
        ("project_name", name),
        ("company_name", "API Owner"),
        ("address", "API Street"),
        ("location", "Lisboa"),
        ("country", "PT"),
        ("timezone", "Europe/Lisbon"),
        ("kwp", "50"),
        ("kwac", "45"),
        ("commissioning_date", "2024-01-15"),
    ]
    if target_asset_id is not None:
        values.append(("target_asset_id", str(target_asset_id)))
    values.extend(("apply_field", field) for field in (apply_fields or []))
    return MultiDict(values)


def configure_provider(conn, provider: str, *, system_ids: str = "") -> None:
    app_module.ensure_integration_seed_data(conn)
    conn.execute(
        """
        UPDATE integration_configs
        SET username = 'test-user', password = 'test-secret', enabled = 1,
            auto_sync_enabled = 0, system_ids = ?
        WHERE provider = ?
        """,
        (system_ids, provider),
    )
    conn.commit()


def test_import_new_sigenergy_creates_asset_mapping_snapshot_and_background_job(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-import.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        configure_provider(conn, "Sigenergy")
        import_id = preview_import(
            conn,
            provider="Sigenergy",
            external_id="SIG-NEW-1",
            name="Sigenergy New Site",
        )
        asset_id, job_id = app_module.apply_installation_import(
            conn,
            import_id,
            confirm_form("Sigenergy New Site"),
        )

        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        mapping = conn.execute(
            "SELECT * FROM asset_integrations WHERE provider = 'Sigenergy' AND external_id = 'SIG-NEW-1'"
        ).fetchone()
        snapshot = conn.execute(
            "SELECT * FROM integration_realtime_snapshots WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        job = conn.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()

        assert asset["project_name"] == "Sigenergy New Site"
        assert asset["data_source"] == "Sigenergy API"
        assert mapping["asset_id"] == asset_id
        assert snapshot["battery_soc_pct"] == 77
        assert job["job_type"] == "sigenergy_state_sync"
        assert json.loads(job["params_json"])["target_external_ids"] == [
            "SIG-NEW-1"
        ]
        assert conn.execute(
            "SELECT auto_sync_enabled FROM integration_configs "
            "WHERE provider = 'Sigenergy'"
        ).fetchone()["auto_sync_enabled"] == 0


def test_import_new_fusionsolar_persists_inverter_details(tmp_path) -> None:
    db_path = tmp_path / "fusion-import.db"
    app_module.ensure_database(str(db_path))
    device = {
        "stationCode": "FS-NEW-1",
        "devId": "INV-001",
        "devName": "Inverter 1",
        "devTypeId": 1,
        "model": "SUN2000-50KTL",
        "ratedPower": 50,
        "sn": "SERIAL-001",
    }
    with get_db(str(db_path)) as conn:
        import_id = preview_import(
            conn,
            provider="FusionSolar",
            external_id="FS-NEW-1",
            name="Fusion New Site",
            raw_payload={"devices": [device]},
        )
        asset_id, job_id = app_module.apply_installation_import(
            conn,
            import_id,
            confirm_form("Fusion New Site"),
        )

        stored = conn.execute(
            "SELECT * FROM provider_devices WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        assert stored["external_device_id"] == "INV-001"
        assert stored["model"] == "SUN2000-50KTL"
        assert stored["sn"] == "SERIAL-001"
        assert conn.execute(
            "SELECT job_type FROM background_jobs WHERE id = ?", (job_id,)
        ).fetchone()["job_type"] == "fusionsolar_state_sync"
        job_params = json.loads(
            conn.execute(
                "SELECT params_json FROM background_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()["params_json"]
        )
        assert job_params["target_external_ids"] == ["FS-NEW-1"]


def test_sigenergy_missing_access_sends_only_one_onboarding_request(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "onboarding-once.db"
    app_module.ensure_database(str(db_path))
    calls = []

    def fake_onboard(config, system_id, session):
        calls.append(system_id)
        return {
            "system_id": system_id,
            "status": "requested",
            "provider_code": "0",
            "message": "pending",
            "response": {"code": 0},
        }

    monkeypatch.setattr(app_module.sigenergy_service, "onboard_system", fake_onboard)
    with get_db(str(db_path)) as conn:
        app_module.ensure_integration_seed_data(conn)
        config = app_module.get_integration_config(conn, "Sigenergy")
        first = app_module.create_sigenergy_onboarding_request(conn, config, "SIG-PENDING")
        second = app_module.create_sigenergy_onboarding_request(conn, config, "SIG-PENDING")
        row = conn.execute(
            "SELECT * FROM sigenergy_onboarding_requests WHERE system_id = 'SIG-PENDING'"
        ).fetchone()
        queue_state = conn.execute(
            """
            SELECT daily_call_count
            FROM production_api_queue_state
            WHERE provider = 'sigenergy' AND api_area = 'discovery'
            """
        ).fetchone()

        assert first["request_id"] == second["request_id"]
        assert second["reused"] is True
        assert calls == ["SIG-PENDING"]
        assert row["attempt_count"] == 1
        assert queue_state["daily_call_count"] == 1


def test_sigenergy_import_list_and_preview_use_the_common_queue(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-import-queue.db"
    app_module.ensure_database(str(db_path))

    class FakeClient:
        def __init__(self, _config, session=None) -> None:
            self.session = session

        def list_systems(self, *, allow_empty: bool = False):
            assert allow_empty is True
            return [
                {
                    "systemId": "SIG-QUEUE",
                    "systemName": "Queued system",
                    "status": "Normal",
                }
            ]

        def get_energy_flow(self, system_id: str):
            assert system_id == "SIG-QUEUE"
            return {"pvPower": 1.5, "loadPower": 0.8}

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        FakeClient,
    )
    with get_db(str(db_path)) as conn:
        configure_provider(conn, "Sigenergy")

        listed = app_module.list_provider_installations_for_import(
            conn,
            "Sigenergy",
        )
        preview, _raw = app_module.fetch_provider_installation_preview(
            conn,
            "Sigenergy",
            "SIG-QUEUE",
        )
        queue_rows = {
            row["api_area"]: int(row["daily_call_count"])
            for row in conn.execute(
                """
                SELECT api_area, daily_call_count
                FROM production_api_queue_state
                WHERE provider = 'sigenergy'
                """
            ).fetchall()
        }

    assert [row["external_id"] for row in listed] == ["SIG-QUEUE"]
    assert preview["external_id"] == "SIG-QUEUE"
    assert queue_rows["discovery"] == 2
    assert queue_rows["state"] == 1


def test_access_check_resumes_pending_import_without_duplicate_data(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "resume-pending.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="SIG-APPROVED",
            external_name="SIG-APPROVED",
            mode="create",
            target_asset_id=None,
            status="access_pending",
            access_status="pending_approval",
            normalized={"external_id": "SIG-APPROVED", "external_name": "SIG-APPROVED"},
            raw_payload={},
            created_by="test",
        )
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO sigenergy_onboarding_requests (
                system_id, requested_at, requested_by, status, provider_code,
                provider_message, attempt_count, response_json, created_at, updated_at,
                installation_import_id
            ) VALUES (?, ?, '', 'requested', '0', 'pending', 1, '{}', ?, ?, ?)
            """,
            ("SIG-APPROVED", now, now, now, import_id),
        )
        conn.commit()

    monkeypatch.setattr(
        app_module,
        "fetch_provider_installation_preview",
        lambda *args, **kwargs: (
            normalized("Sigenergy", "SIG-APPROVED", "Approved Site"),
            {"system": {"systemId": "SIG-APPROVED"}},
        ),
    )
    monkeypatch.setattr(app_module, "schedule_background_job", lambda *args, **kwargs: True)
    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.post(
            f"/integrations/import/{import_id}/check-access",
            data={"csrf_token": "token"},
        )
    finally:
        flask_app.config["DATABASE"] = original_database

    assert response.status_code == 302
    with get_db(str(db_path)) as conn:
        queued = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?", (import_id,)
        ).fetchone()
        assert queued["status"] == "queued"
        result = app_module.run_installation_import_preview_job(
            conn,
            import_id,
        )
        imported = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?", (import_id,)
        ).fetchone()
        onboarding = conn.execute(
            "SELECT * FROM sigenergy_onboarding_requests WHERE installation_import_id = ?",
            (import_id,),
        ).fetchone()
        assert result["status"] == "preview"
        assert result["access_status"] == "available"
        assert imported["status"] == "preview"
        assert imported["access_status"] == "available"
        assert onboarding["status"] == "approved"
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


def test_associate_existing_asset_does_not_overwrite_manual_fields_by_default(tmp_path) -> None:
    db_path = tmp_path / "associate.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO assets (project_name, company_name, address, kwp)
            VALUES ('Manual Site', 'Manual Owner', 'Manual Address', '99')
            """
        )
        asset_id = int(cursor.lastrowid)
        conn.commit()
        import_id = preview_import(
            conn,
            provider="FusionSolar",
            external_id="FS-ASSOC",
            name="Manual Site",
            mode="associate",
            target_asset_id=asset_id,
        )
        linked_asset_id, _ = app_module.apply_installation_import(
            conn,
            import_id,
            confirm_form("API Site", target_asset_id=asset_id),
        )
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

        assert linked_asset_id == asset_id
        assert asset["project_name"] == "Manual Site"
        assert asset["company_name"] == "Manual Owner"
        assert asset["address"] == "Manual Address"
        assert asset["kwp"] == "99"


def test_ambiguous_exact_names_never_choose_asset_automatically(tmp_path) -> None:
    db_path = tmp_path / "ambiguous.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        conn.execute("INSERT INTO assets (project_name) VALUES ('Same Site')")
        conn.execute("INSERT INTO assets (project_name) VALUES ('Same Site')")
        conn.commit()
        import_id = preview_import(
            conn,
            provider="FusionSolar",
            external_id="FS-AMBIG",
            name="Same Site",
            mode="associate",
        )
        context = app_module.get_installation_import_context(conn, import_id)

        assert context["ambiguous_match"] is True
        assert context["suggested_asset_id"] is None


def test_failed_finalization_rolls_back_asset_mapping_and_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "atomic.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        import_id = preview_import(
            conn,
            provider="FusionSolar",
            external_id="FS-ROLLBACK",
            name="Rollback Site",
        )
        monkeypatch.setattr(
            app_module,
            "create_background_job",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("job failed")),
        )
        try:
            app_module.apply_installation_import(
                conn,
                import_id,
                confirm_form("Rollback Site"),
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected finalization failure")

        assert conn.execute(
            "SELECT COUNT(*) FROM assets WHERE project_name = 'Rollback Site'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM asset_integrations WHERE external_id = 'FS-ROLLBACK'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM background_jobs"
        ).fetchone()[0] == 0


def test_repeating_same_import_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "idempotent.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        import_id = preview_import(
            conn,
            provider="Sigenergy",
            external_id="SIG-IDEMP",
            name="Idempotent Site",
        )
        first_asset, first_job = app_module.apply_installation_import(
            conn,
            import_id,
            confirm_form("Idempotent Site"),
        )
        second_asset, second_job = app_module.apply_installation_import(
            conn,
            import_id,
            confirm_form("Idempotent Site"),
        )

        assert first_asset == second_asset
        assert first_job == second_job
        assert conn.execute(
            "SELECT COUNT(*) FROM assets WHERE project_name = 'Idempotent Site'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM asset_integrations WHERE provider = 'Sigenergy' AND external_id = 'SIG-IDEMP'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE job_type = 'sigenergy_state_sync'"
        ).fetchone()[0] == 1


def test_import_route_uses_preview_and_never_stores_secret_in_payload(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "route-import.db"
    app_module.ensure_database(str(db_path))
    secret = "must-not-be-stored"
    monkeypatch.setattr(app_module, "schedule_background_job", lambda *args, **kwargs: True)
    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.post(
            "/integrations/import",
            data={
                "csrf_token": "token",
                "provider": "Sigenergy",
                "mode": "create",
                "external_id": "SIG-ROUTE",
                "seed_json": json.dumps(
                    {"Authorization": f"Bearer {secret}", "appSecret": secret}
                ),
                "action": "preview",
            },
        )
    finally:
        flask_app.config["DATABASE"] = original_database

    assert response.status_code == 302
    import_id = int(parse_qs(urlparse(response.headers["Location"]).query)["import_id"][0])
    with get_db(str(db_path)) as conn:
        row = conn.execute(
            """
            SELECT bj.params_json
            FROM installation_imports ii
            JOIN background_jobs bj ON bj.id = ii.background_job_id
            WHERE ii.id = ?
            """,
            (import_id,),
        ).fetchone()
        assert secret not in row["params_json"]
        assert "[redacted]" in row["params_json"]


def test_environment_credentials_keep_priority_during_import(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "env-priority.db"
    app_module.ensure_database(str(db_path))
    monkeypatch.setenv("SIGENERGY_APP_KEY", "env-key")
    monkeypatch.setenv("SIGENERGY_APP_SECRET", "env-secret")
    with get_db(str(db_path)) as conn:
        app_module.ensure_integration_seed_data(conn)
        conn.execute(
            "UPDATE integration_configs SET username = 'db-key', password = 'db-secret' WHERE provider = 'Sigenergy'"
        )
        conn.commit()
        config = app_module.get_integration_config(conn, "Sigenergy")

        assert config["username"] == "env-key"
        assert config["password"] == "env-secret"
        assert config["password_source"] == "env"


def test_import_pages_render_primary_flow_and_existing_asset_comparison(tmp_path) -> None:
    db_path = tmp_path / "import-ui.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        cursor = conn.execute(
            "INSERT INTO assets (project_name, company_name, kwp) VALUES ('Compare Site', 'Manual Owner', '99')"
        )
        asset_id = int(cursor.lastrowid)
        conn.commit()
        import_id = preview_import(
            conn,
            provider="FusionSolar",
            external_id="FS-COMPARE",
            name="Compare Site",
            mode="associate",
            target_asset_id=asset_id,
        )

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        start_response = client.get("/integrations/import")
        preview_response = client.get(f"/integrations/import?import_id={import_id}")
    finally:
        flask_app.config["DATABASE"] = original_database

    assert start_response.status_code == 200
    assert "Importar instalação" in start_response.get_data(as_text=True)
    assert preview_response.status_code == 200
    html = preview_response.get_data(as_text=True)
    assert "Valor atual" in html
    assert "Manual Owner" in html
    assert "Associar e sincronizar" in html


def test_import_discovery_respects_existing_provider_cooldown(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "import-cooldown.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        app_module.ensure_integration_seed_data(conn)
        conn.execute(
            """
            UPDATE integration_configs
            SET username = 'user', password = 'secret'
            WHERE provider = 'FusionSolar'
            """
        )
        app_module.mark_api_cooldown(
            conn,
            "FusionSolar",
            app_module.API_AREA_STATE,
            "fixture cooldown",
        )
        conn.commit()
        monkeypatch.setattr(
            app_module,
            "build_fusionsolar_client",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("API client must not be created during cooldown")
            ),
        )

        with pytest.raises(app_module.ApiRateLimitError):
            app_module.list_provider_installations_for_import(conn, "FusionSolar")


def test_sigenergy_access_pending_requires_explicit_onboarding_and_creates_no_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "onboarding-explicit.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        app_module.ensure_integration_seed_data(conn)
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="SIG-FAIL",
            external_name="SIG-FAIL",
            mode="create",
            target_asset_id=None,
            status="queued",
            access_status="checking",
            normalized={"external_id": "SIG-FAIL", "external_name": "SIG-FAIL"},
            raw_payload={},
            created_by="test",
        )
        conn.commit()
        monkeypatch.setattr(
            app_module,
            "fetch_provider_installation_preview",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                app_module.SigenergyInstallationAccessPending("pending")
            ),
        )
        result = app_module.run_installation_import_preview_job(conn, import_id)

        row = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?", (import_id,)
        ).fetchone()
        assert result["status"] == "access_pending"
        assert result["access_status"] == "not_returned"
        assert result["message"] == (
            "A instalação não foi devolvida pela App Key configurada."
        )
        assert result["onboarding_required"] is True
        assert row["status"] == "access_pending"
        assert row["access_status"] == "not_returned"
        assert row["last_error"] == result["message"]
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM asset_integrations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sigenergy_onboarding_requests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "request_status",
    [
        "requested",
        "already_requested",
        "already_requested_or_onboarded",
    ],
)
def test_sigenergy_missing_system_with_real_onboarding_stays_pending_approval(
    tmp_path,
    monkeypatch,
    request_status,
) -> None:
    db_path = tmp_path / f"onboarding-{request_status}.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="SIG-PENDING",
            external_name="SIG-PENDING",
            mode="create",
            target_asset_id=None,
            status="queued",
            access_status="checking",
            normalized={
                "external_id": "SIG-PENDING",
                "external_name": "SIG-PENDING",
            },
            raw_payload={},
            created_by="test",
        )
        now = datetime.now().isoformat(timespec="seconds")
        request_id = int(
            conn.execute(
                """
                INSERT INTO sigenergy_onboarding_requests (
                    system_id, requested_at, requested_by, status,
                    attempt_count, response_json, created_at, updated_at,
                    installation_import_id
                ) VALUES (?, ?, '', ?, 1, '{}', ?, ?, ?)
                """,
                (
                    "SIG-PENDING",
                    now,
                    request_status,
                    now,
                    now,
                    import_id,
                ),
            ).lastrowid
        )
        conn.commit()
        monkeypatch.setattr(
            app_module,
            "fetch_provider_installation_preview",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                app_module.SigenergyInstallationAccessPending("pending")
            ),
        )

        result = app_module.run_installation_import_preview_job(
            conn,
            import_id,
        )
        row = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?",
            (import_id,),
        ).fetchone()

    assert result["access_status"] == "pending_approval"
    assert result["onboarding_required"] is False
    assert result["onboarding_request_id"] == request_id
    assert result["onboarding_reused"] is True
    assert row["access_status"] == "pending_approval"
    assert row["onboarding_request_id"] == request_id


@pytest.mark.parametrize(
    "available_systems",
    [
        [],
        [{"systemId": "SIG-OTHER", "systemName": "Outra instalação"}],
    ],
    ids=["empty-list", "requested-id-absent"],
)
def test_sigenergy_missing_from_discovery_uses_direct_energy_flow_without_onboarding(
    tmp_path,
    monkeypatch,
    available_systems,
) -> None:
    db_path = tmp_path / "sigenergy-access-pending.db"
    app_module.ensure_database(str(db_path))
    list_calls: list[bool] = []

    class FakeClient:
        def __init__(self, _config, session=None) -> None:
            self.session = session

        def list_systems(self, *, allow_empty: bool = False):
            list_calls.append(allow_empty)
            return available_systems

        def get_energy_flow(self, _system_id: str):
            assert _system_id == "TZXRS1780315946"
            return {
                "pvPower": 4.2,
                "loadPower": 1.5,
                "batterySoc": 73,
            }

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        FakeClient,
    )
    monkeypatch.setattr(
        app_module,
        "create_sigenergy_onboarding_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Onboarding must never be requested automatically")
        ),
    )
    with get_db(str(db_path)) as conn:
        configure_provider(conn, "Sigenergy")
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="TZXRS1780315946",
            external_name="Expertcom",
            mode="create",
            target_asset_id=None,
            status="queued",
            access_status="checking",
            normalized={
                "external_id": "TZXRS1780315946",
                "external_name": "Expertcom",
            },
            raw_payload={},
            created_by="test",
        )
        conn.commit()

        first = app_module.run_installation_import_preview_job(conn, import_id)
        second = app_module.run_installation_import_preview_job(conn, import_id)
        installation_import = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?",
            (import_id,),
        ).fetchone()
        onboarding_count = conn.execute(
            "SELECT COUNT(*) FROM sigenergy_onboarding_requests"
        ).fetchone()[0]
        inventory = conn.execute(
            """
            SELECT * FROM provider_system_inventory
            WHERE provider = 'Sigenergy' AND external_id = 'TZXRS1780315946'
            """
        ).fetchone()
        validations = conn.execute(
            """
            SELECT * FROM sigenergy_access_validations
            WHERE system_id = 'TZXRS1780315946'
            ORDER BY id
            """
        ).fetchall()
        asset_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    assert first["status"] == second["status"] == "preview"
    assert first["validation_method"] == "direct_energy_flow"
    assert list_calls == [True, True]
    assert installation_import["status"] == "preview"
    assert installation_import["access_status"] == "available"
    assert installation_import["validation_method"] == "direct_energy_flow"
    assert installation_import["external_name"] == "Expertcom"
    assert "validação direta energyFlow" in installation_import["source_label"]
    assert installation_import["onboarding_request_id"] is None
    assert inventory["access_status"] == "accessible"
    assert inventory["validation_method"] == "direct_energy_flow"
    assert inventory["external_name"] == "Expertcom"
    assert [row["outcome"] for row in validations] == [
        "available",
        "available",
    ]
    assert onboarding_count == 0
    assert asset_count == 0

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.get(f"/integrations/import?import_id={import_id}")
    finally:
        flask_app.config["DATABASE"] = original_database
    html = response.get_data(as_text=True)
    normalized_html = " ".join(html.split())
    assert response.status_code == 200
    assert "Expertcom" in html
    assert "TZXRS1780315946" in html
    assert "Acesso disponível" in html
    assert "validado diretamente por energyFlow" in html
    assert "não foi devolvida por /openapi/system" in normalized_html


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_sigenergy_direct_validation_failure_creates_no_asset_or_onboarding(
    tmp_path,
    monkeypatch,
    status_code,
) -> None:
    db_path = tmp_path / f"sigenergy-direct-{status_code}.db"
    app_module.ensure_database(str(db_path))

    class FakeClient:
        def __init__(self, _config, session=None) -> None:
            self.session = session

        def list_systems(self, *, allow_empty: bool = False):
            assert allow_empty is True
            return []

        def get_energy_flow(self, _system_id: str):
            raise SigenergyApiError(
                f"Sigenergy HTTP {status_code} Bearer secret-token",
                status_code=status_code,
            )

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        FakeClient,
    )
    monkeypatch.setattr(
        app_module,
        "create_sigenergy_onboarding_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Onboarding must never be requested automatically")
        ),
    )
    with get_db(str(db_path)) as conn:
        configure_provider(conn, "Sigenergy")
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="TZXRS1780315946",
            external_name="Expertcom",
            mode="create",
            target_asset_id=None,
            status="queued",
            access_status="checking",
            normalized={
                "external_id": "TZXRS1780315946",
                "external_name": "Expertcom",
            },
            raw_payload={},
            created_by="test",
        )
        conn.commit()

        with pytest.raises(SigenergyApiError):
            app_module.run_installation_import_preview_job(conn, import_id)

        installation_import = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?",
            (import_id,),
        ).fetchone()
        validation = conn.execute(
            "SELECT * FROM sigenergy_access_validations"
        ).fetchone()

        assert installation_import["status"] == "error"
        assert installation_import["access_status"] == "error"
        assert "secret-token" not in installation_import["last_error"]
        assert validation["outcome"] == "failed"
        assert validation["status_code"] == status_code
        assert "secret-token" not in validation["sanitized_error"]
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM asset_integrations"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sigenergy_onboarding_requests"
        ).fetchone()[0] == 0


def test_sigenergy_not_returned_ui_never_claims_request_was_sent(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-not-returned-ui.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="SIG-NOT-RETURNED",
            external_name="SIG-NOT-RETURNED",
            mode="create",
            target_asset_id=None,
            status="queued",
            access_status="checking",
            normalized={
                "external_id": "SIG-NOT-RETURNED",
                "external_name": "SIG-NOT-RETURNED",
            },
            raw_payload={},
            created_by="test",
        )
        monkeypatch.setattr(
            app_module,
            "fetch_provider_installation_preview",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                app_module.SigenergyInstallationAccessPending("pending")
            ),
        )
        app_module.run_installation_import_preview_job(conn, import_id)

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.get(
            f"/integrations/import?import_id={import_id}"
        )
    finally:
        flask_app.config["DATABASE"] = original_database

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Não devolvida pela App Key" in html
    assert "A instalação não foi devolvida pela App Key configurada." in html
    assert "Pedido enviado — aguarda aprovação" not in html


def test_installation_import_ui_uses_human_access_status_labels(
    tmp_path,
) -> None:
    db_path = tmp_path / "installation-access-labels.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        for index, access_status in enumerate(
            ("not_returned", "pending_approval", "available", "error"),
            start=1,
        ):
            app_module.upsert_installation_import_preview(
                conn,
                provider="Sigenergy",
                external_id=f"SIG-LABEL-{index}",
                external_name=f"Label {index}",
                mode="create",
                target_asset_id=None,
                status=(
                    "preview"
                    if access_status == "available"
                    else (
                        "error"
                        if access_status == "error"
                        else "access_pending"
                    )
                ),
                access_status=access_status,
                normalized={"external_id": f"SIG-LABEL-{index}"},
                raw_payload={},
                created_by="test",
            )
        conn.commit()

    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.get("/integrations")
    finally:
        flask_app.config["DATABASE"] = original_database

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    for label in (
        "Não devolvida pela App Key",
        "Pedido enviado — aguarda aprovação",
        "Acesso disponível",
        "Erro na verificação",
    ):
        assert label in html


def test_sigenergy_pending_approval_migration_preserves_real_requests(
    tmp_path,
) -> None:
    db_path = tmp_path / "sigenergy-access-status-migration.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        without_request = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="SIG-WITHOUT-REQUEST",
            external_name="SIG-WITHOUT-REQUEST",
            mode="create",
            target_asset_id=None,
            status="access_pending",
            access_status="pending_approval",
            normalized={"external_id": "SIG-WITHOUT-REQUEST"},
            raw_payload={},
            created_by="test",
        )
        with_request = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="SIG-WITH-REQUEST",
            external_name="SIG-WITH-REQUEST",
            mode="create",
            target_asset_id=None,
            status="access_pending",
            access_status="pending_approval",
            normalized={"external_id": "SIG-WITH-REQUEST"},
            raw_payload={},
            created_by="test",
        )
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO sigenergy_onboarding_requests (
                system_id, requested_at, status, attempt_count,
                response_json, created_at, updated_at, installation_import_id
            ) VALUES ('SIG-WITH-REQUEST', ?, 'requested', 1, '{}', ?, ?, ?)
            """,
            (now, now, now, with_request),
        )
        conn.commit()

    app_module.ensure_database(str(db_path))
    app_module.ensure_database(str(db_path))

    with get_db(str(db_path)) as conn:
        migrated = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?",
            (without_request,),
        ).fetchone()
        preserved = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?",
            (with_request,),
        ).fetchone()

    assert migrated["access_status"] == "not_returned"
    assert migrated["last_error"] == (
        "A instalação não foi devolvida pela App Key configurada."
    )
    assert preserved["access_status"] == "pending_approval"


def test_sigenergy_accessible_import_syncs_only_new_mapping_despite_fixed_ids(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-targeted-sync.db"
    app_module.ensure_database(str(db_path))
    client_configs: list[object] = []

    class FakeClient:
        def __init__(self, config, session=None) -> None:
            client_configs.append(config.get("system_ids"))
            self.system_ids = config.get("system_ids")
            self.session = session

        def list_systems(self, *, allow_empty: bool = False):
            if allow_empty:
                return [
                    {
                        "systemId": "SIG-NEW",
                        "systemName": "Nova Sigenergy",
                        "status": "Normal",
                        "address": "Rua API",
                    }
                ]
            return [
                {
                    "systemId": item,
                    "systemName": item,
                    "status": "Normal",
                }
                for item in self.system_ids
            ]

        def get_energy_flow(self, system_id: str):
            assert system_id == "SIG-NEW"
            return {"pvPower": 4.5, "loadPower": 1.0}

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        FakeClient,
    )
    with get_db(str(db_path)) as conn:
        configure_provider(
            conn,
            "Sigenergy",
            system_ids="SIG-LEGACY-1,SIG-LEGACY-2",
        )
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="Sigenergy",
            external_id="SIG-NEW",
            external_name="SIG-NEW",
            mode="create",
            target_asset_id=None,
            status="queued",
            access_status="checking",
            normalized={
                "external_id": "SIG-NEW",
                "external_name": "SIG-NEW",
            },
            raw_payload={},
            created_by="test",
        )
        conn.commit()
        app_module.run_installation_import_preview_job(conn, import_id)
        asset_id, job_id = app_module.apply_installation_import(
            conn,
            import_id,
            confirm_form("Nova Sigenergy"),
        )
        job = conn.execute(
            "SELECT params_json FROM background_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        result = app_module.run_sigenergy_sync(
            conn,
            "Sigenergy",
            trigger_type="installation_import",
            target_external_ids=["SIG-NEW"],
        )
        snapshots = conn.execute(
            """
            SELECT external_id
            FROM integration_realtime_snapshots
            WHERE provider = 'Sigenergy' AND asset_id = ?
            ORDER BY id
            """,
            (asset_id,),
        ).fetchall()

    assert json.loads(job["params_json"])["target_external_ids"] == ["SIG-NEW"]
    assert client_configs[0] == ""
    assert client_configs[-1] == ""
    assert result["matched"] == 1
    assert {row["external_id"] for row in snapshots} == {"SIG-NEW"}


def test_sigenergy_normal_sync_uses_full_discovery_and_ignores_manual_ids(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-active-mappings.db"
    app_module.ensure_database(str(db_path))
    configured_ids: list[object] = []

    class FakeClient:
        def __init__(self, config, session=None) -> None:
            configured_ids.append(config.get("system_ids"))
            self.system_ids = config.get("system_ids")
            self.session = session

        def list_systems(self, *, allow_empty: bool = False):
            return [
                {"systemId": "SIG-ACTIVE", "systemName": "Active", "status": "Normal"},
                {"systemId": "SIG-UNMAPPED", "systemName": "Unmapped", "status": "Normal"},
            ]

        def get_energy_flow(self, _system_id: str):
            return {"pvPower": 1.0}

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        FakeClient,
    )
    with get_db(str(db_path)) as conn:
        configure_provider(
            conn,
            "Sigenergy",
            system_ids="SIG-LEGACY,SIG-UNMAPPED",
        )
        active_asset = conn.execute(
            "INSERT INTO assets (project_name) VALUES ('Active')"
        ).lastrowid
        disabled_asset = conn.execute(
            "INSERT INTO assets (project_name) VALUES ('Disabled')"
        ).lastrowid
        conn.execute(
            """
            INSERT INTO asset_integrations (
                asset_id, provider, external_id, external_name, enabled
            ) VALUES (?, 'Sigenergy', 'SIG-ACTIVE', 'Active', 1)
            """,
            (active_asset,),
        )
        conn.execute(
            """
            INSERT INTO asset_integrations (
                asset_id, provider, external_id, external_name, enabled
            ) VALUES (?, 'Sigenergy', 'SIG-DISABLED', 'Disabled', 0)
            """,
            (disabled_asset,),
        )
        conn.commit()
        result = app_module.run_sigenergy_check(
            conn,
            "Sigenergy",
            dry_run=True,
        )

    assert configured_ids == [""]
    assert [row["external_id"] for row in result["rows"]] == [
        "SIG-ACTIVE",
        "SIG-UNMAPPED",
    ]


def test_fusionsolar_raw_devices_are_normalized_for_realtime_and_storage(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "fusionsolar-raw-device.db"
    app_module.ensure_database(str(db_path))
    realtime_requests: list[list[dict]] = []
    raw_device = {
        "stationCode": "FS-RAW",
        "devId": "INV-RAW",
        "devTypeId": 1,
        "devName": "Inversor API",
        "model": "SUN2000-50KTL",
        "sn": "SERIAL-RAW",
        "ratedPower": 50000,
    }
    realtime_payload = {
        "devId": "INV-RAW",
        "dataItemMap": {"inverter_state": "512", "active_power": 12.5},
    }

    class FakeClient:
        def station_realtime_kpi(self, station_codes):
            assert station_codes == ["FS-RAW"]
            return {
                "FS-RAW": {
                    "stationCode": "FS-RAW",
                    "dataItemMap": {"active_power": 20},
                }
            }

        def device_list(self, station_codes):
            assert station_codes == ["FS-RAW"]
            return [raw_device]

        def device_realtime_kpi(self, devices):
            realtime_requests.append(devices)
            assert devices[0]["external_device_id"] == "INV-RAW"
            assert devices[0]["dev_type_id"] == 1
            return {"INV-RAW": realtime_payload}

        def alarms(self, station_codes):
            assert station_codes == ["FS-RAW"]
            return {"FS-RAW": []}

    monkeypatch.setattr(
        app_module,
        "build_fusionsolar_client",
        lambda _config: FakeClient(),
    )
    with get_db(str(db_path)) as conn:
        configure_provider(conn, "FusionSolar")
        normalized_preview, raw_payload = (
            app_module.fetch_provider_installation_preview(
                conn,
                "FusionSolar",
                "FS-RAW",
                seed={
                    "plantCode": "FS-RAW",
                    "plantName": "Central Raw",
                    "capacity": 75,
                },
            )
        )
        import_id = app_module.upsert_installation_import_preview(
            conn,
            provider="FusionSolar",
            external_id="FS-RAW",
            external_name="Central Raw",
            mode="create",
            target_asset_id=None,
            status="preview",
            access_status="available",
            normalized=normalized_preview,
            raw_payload=raw_payload,
            created_by="test",
        )
        conn.commit()
        asset_id, _ = app_module.apply_installation_import(
            conn,
            import_id,
            confirm_form("Central Raw"),
        )
        stored = conn.execute(
            "SELECT * FROM provider_devices WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        stored_payload = json.loads(stored["payload_json"])

    preview_device = normalized_preview["devices"][0]
    assert realtime_requests
    assert raw_payload["devices"][0]["ratedPower"] == 50000
    assert preview_device["model"] == "SUN2000-50KTL"
    assert preview_device["serial_number"] == "SERIAL-RAW"
    assert preview_device["rated_power_kw"] == 50
    assert preview_device["status"] == "512"
    assert stored["rated_power_kw"] == 50
    assert stored_payload["device"]["ratedPower"] == 50000
    assert stored_payload["realtime"] == realtime_payload


def test_scheduler_failure_does_not_leave_installation_import_pending(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "scheduler-failure.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        configure_provider(conn, "Sigenergy")
    monkeypatch.setattr(
        app_module,
        "schedule_background_job",
        lambda *args, **kwargs: False,
    )
    flask_app, original_database, client = authenticated_client(db_path)
    try:
        response = client.post(
            "/integrations/import",
            data={
                "csrf_token": "token",
                "provider": "Sigenergy",
                "mode": "create",
                "external_id": "SIG-NO-SCHEDULER",
                "action": "preview",
            },
        )
        import_id = int(
            parse_qs(urlparse(response.headers["Location"]).query)["import_id"][0]
        )
        retry_page = client.get(
            f"/integrations/import?import_id={import_id}"
        )
    finally:
        flask_app.config["DATABASE"] = original_database

    with get_db(str(db_path)) as conn:
        installation_import = conn.execute(
            "SELECT * FROM installation_imports WHERE id = ?",
            (import_id,),
        ).fetchone()
        job = conn.execute(
            "SELECT * FROM background_jobs WHERE id = ?",
            (installation_import["background_job_id"],),
        ).fetchone()

    assert installation_import["status"] == "error"
    assert installation_import["access_status"] == "error"
    assert job["status"] == "failed"
    assert "scheduler" in installation_import["last_error"]
    assert "Repetir recolha" in retry_page.get_data(as_text=True)


def test_auto_sync_is_enabled_only_after_final_confirmation(
    tmp_path,
) -> None:
    db_path = tmp_path / "auto-sync-confirmation.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        configure_provider(conn, "Sigenergy")
        import_id = preview_import(
            conn,
            provider="Sigenergy",
            external_id="SIG-AUTO",
            name="Auto Sync",
        )
        before = conn.execute(
            "SELECT auto_sync_enabled FROM integration_configs "
            "WHERE provider = 'Sigenergy'"
        ).fetchone()["auto_sync_enabled"]
        form = confirm_form("Auto Sync")
        form.add("enable_auto_sync", "on")
        app_module.apply_installation_import(conn, import_id, form)
        after = conn.execute(
            "SELECT auto_sync_enabled FROM integration_configs "
            "WHERE provider = 'Sigenergy'"
        ).fetchone()["auto_sync_enabled"]

    assert before == 0
    assert after == 1
