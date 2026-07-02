from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.services import sigenergy as sigenergy_service


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sigenergy"


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def clear_sigenergy_token_cache() -> None:
    sigenergy_service.clear_token_cache_for_tests()
    yield
    sigenergy_service.clear_token_cache_for_tests()


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="ascii"))


def sigenergy_config() -> dict[str, Any]:
    return {
        "username": "fake-app-key",
        "password": "fake-app-secret",
        "base_url": "https://sigenergy.example.test",
        "login_endpoint": "/openapi/auth/login/key",
        "plants_endpoint": "/openapi/system",
        "systems_endpoint": "/openapi/system",
        "energy_flow_endpoint": "/openapi/systems/{system_id}/energyFlow",
        "onboard_endpoint": "/openapi/board/onboard",
        "region": "eu",
        "system_ids": "",
    }


def insert_enabled_sigenergy_config(conn) -> None:
    now = app_module.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR REPLACE INTO integration_configs (
            provider, username, password, base_url, login_endpoint, plants_endpoint,
            energy_flow_endpoint, onboard_endpoint, enabled, auto_sync_enabled,
            sync_hours, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
        """,
        (
            app_module.INTEGRATION_PROVIDER_SIGENERGY,
            "fake-app-key",
            "fake-app-secret",
            "https://sigenergy.example.test",
            "/openapi/auth/login/key",
            "/openapi/system",
            "/openapi/systems/{system_id}/energyFlow",
            "/openapi/board/onboard",
            "08:00",
            now,
            now,
        ),
    )
    conn.commit()


def test_parse_provider_payload_data_supports_json_string_object_and_invalid_string() -> None:
    json_string_payload = load_fixture("auth_success_json_string.json")
    object_payload = load_fixture("auth_success_object.json")

    assert app_module.parse_provider_payload_data(json_string_payload)["accessToken"] == (
        "fake-sigenergy-token-json-string"
    )
    assert app_module.parse_provider_payload_data(object_payload) == object_payload["data"]
    assert app_module.parse_provider_payload_data({"data": "not-json"}) == "not-json"


@pytest.mark.parametrize(
    ("fixture_name", "expected_token"),
    [
        ("auth_success_json_string.json", "fake-sigenergy-token-json-string"),
        ("auth_success_object.json", "fake-sigenergy-token-object"),
    ],
)
def test_get_sigenergy_token_accepts_json_string_and_object_payloads(
    monkeypatch,
    fixture_name: str,
    expected_token: str,
) -> None:
    payload = load_fixture(fixture_name)
    post_calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        post_calls.append({"url": url, **kwargs})
        return FakeResponse(payload)

    session = type("FakeSession", (), {"post": staticmethod(fake_post)})()

    token = sigenergy_service.get_access_token(sigenergy_config(), session=session, force_login=True)

    assert token == expected_token
    assert post_calls[0]["url"] == "https://sigenergy.example.test/openapi/auth/login/key"
    assert post_calls[0]["json"]["key"] == "ZmFrZS1hcHAta2V5OmZha2UtYXBwLXNlY3JldA=="
    assert post_calls[0]["headers"]["Accept"] == "application/json"
    assert post_calls[0]["headers"]["Content-Type"] == "application/json"
    assert post_calls[0]["headers"]["sigen-region"] == "eu"


def test_get_sigenergy_token_reuses_cache(monkeypatch) -> None:
    payload = load_fixture("auth_success_object.json")
    post_calls: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        post_calls.append(url)
        return FakeResponse(payload)

    session = type("FakeSession", (), {"post": staticmethod(fake_post)})()

    assert sigenergy_service.get_access_token(sigenergy_config(), session=session) == "fake-sigenergy-token-object"
    assert sigenergy_service.get_access_token(sigenergy_config(), session=session) == "fake-sigenergy-token-object"
    assert len(post_calls) == 1


def test_normalize_sigenergy_system_rows_accepts_list_variants_and_single_object() -> None:
    systems_data = app_module.parse_provider_payload_data(load_fixture("systems_list.json"))

    assert [row["systemId"] for row in app_module.normalize_sigenergy_system_rows(systems_data)] == [
        "SIG-001",
        "SIG-002",
    ]
    assert app_module.normalize_sigenergy_system_rows([{"systemId": "SIG-003"}, "skip"]) == [
        {"systemId": "SIG-003"}
    ]
    assert app_module.normalize_sigenergy_system_rows({"records": [{"systemId": "SIG-004"}]}) == [
        {"systemId": "SIG-004"}
    ]
    assert app_module.normalize_sigenergy_system_rows({"systems": [{"systemId": "SIG-005"}]}) == [
        {"systemId": "SIG-005"}
    ]
    assert app_module.normalize_sigenergy_system_rows({"systemList": [{"systemId": "SIG-006"}]}) == [
        {"systemId": "SIG-006"}
    ]
    assert app_module.normalize_sigenergy_system_rows({"rows": [{"systemId": "SIG-007"}]}) == [
        {"systemId": "SIG-007"}
    ]
    assert app_module.normalize_sigenergy_system_rows({"systemId": "SIG-008", "systemName": "Single"}) == [
        {"systemId": "SIG-008", "systemName": "Single"}
    ]


def test_normalize_sigenergy_system_row_maps_realtime_and_energy_flow_fields() -> None:
    system = app_module.parse_provider_payload_data(load_fixture("systems_list.json"))["list"][0]
    realtime = app_module.parse_provider_payload_data(load_fixture("realtime_data.json"))
    energy_flow = app_module.parse_provider_payload_data(load_fixture("energy_flow.json"))

    row = app_module.normalize_sigenergy_system_row(system, realtime, energy_flow)

    assert row["external_id"] == "SIG-001"
    assert row["external_name"] == "Sigenergy Test Site 1"
    assert row["status"] == "Operacional"
    assert row["raw_status"] == "running"
    assert "system_status=running" in row["notes"]
    assert "pvPower=4.25" in row["notes"]
    assert "batterySoc=78" in row["notes"]
    assert row["grid_power_kw_raw"] == -0.5
    assert row["payload"] == {
        "system": system,
        "realtime": realtime,
        "energy_flow": energy_flow,
    }


def test_sigenergy_notes_show_battery_na_when_capacity_zero() -> None:
    row = app_module.normalize_sigenergy_system_row(
        {"systemId": "SIG-001", "systemName": "No Battery", "status": "Normal", "batteryCapacity": 0},
        {},
        {"pvPower": 2.0, "loadPower": 1.0, "gridPower": -0.25, "batteryPower": 0, "batterySoc": 0},
    )

    assert row["status"] == "Operacional"
    assert "Rede: -0.25 kW" in row["notes"]
    assert "Bateria: N/A" in row["notes"]
    assert "SOC: N/A" in row["notes"]


def test_run_sigenergy_check_continues_after_single_energy_flow_failure(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sigenergy-check.db"
    app_module.ensure_database(str(db_path))

    class FakeClient:
        def __init__(self, _config, session=None) -> None:
            self.session = session

        def list_systems(self) -> list[dict[str, Any]]:
            return [
                {"systemId": "SIG-001", "systemName": "A", "status": "Normal"},
                {"systemId": "SIG-002", "systemName": "B", "status": "Offline"},
            ]

        def get_energy_flow(self, system_id: str) -> dict[str, Any]:
            if system_id == "SIG-002":
                raise ValueError("energy flow unavailable")
            return {"pvPower": 1.0}

    monkeypatch.setattr(sigenergy_service, "SigenergyClient", FakeClient)

    with get_db(str(db_path)) as conn:
        insert_enabled_sigenergy_config(conn)
        result = app_module.run_sigenergy_check(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY, dry_run=True)
        snapshot_count = conn.execute("SELECT COUNT(*) AS total FROM integration_realtime_snapshots").fetchone()["total"]

    assert result["station_count"] == 2
    assert result["realtime_count"] == 1
    assert result["failed_realtime_count"] == 1
    assert len(result["rows"]) == 2
    assert result["rows"][1]["status"] == "Sem dados"
    assert snapshot_count == 0


def _sigenergy_sync_row(external_name: str = "Plant A") -> dict[str, Any]:
    return {
        "external_id": "SIG-001",
        "external_name": external_name,
        "status": "Operacional",
        "raw_status": "Normal",
        "notes": "PV: 4 kW | Carga: 1 kW | Rede: -0.5 kW | Bateria: N/A | SOC: N/A",
        "pv_power_kw": 4.0,
        "load_power_kw": 1.0,
        "grid_power_kw_raw": -0.5,
        "battery_power_kw": 0.0,
        "battery_soc_pct": 0.0,
        "ev_power_kw": None,
        "ac_power_kw": None,
        "heat_pump_power_kw": None,
        "pv_capacity_kw": 10.0,
        "battery_capacity_kwh": 0.0,
        "payload": {"system": {"systemId": "SIG-001"}, "energy_flow": {"gridPower": -0.5}},
    }


def test_run_sigenergy_sync_writes_snapshot_and_monitoring_for_existing_mapping(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sigenergy-sync.db"
    app_module.ensure_database(str(db_path))

    monkeypatch.setattr(
        app_module,
        "run_provider_check",
        lambda _conn, _provider, dry_run=True: {
            "rows": [_sigenergy_sync_row()],
            "station_count": 1,
            "realtime_count": 1,
            "failed_realtime_count": 0,
        },
    )

    with get_db(str(db_path)) as conn:
        insert_enabled_sigenergy_config(conn)
        asset_id = conn.execute("INSERT INTO assets (project_name, active_contract) VALUES ('Plant A', 'yes')").lastrowid
        conn.execute(
            "INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled) VALUES (?, 'Sigenergy', 'SIG-001', 'Plant A', 1)",
            (asset_id,),
        )
        conn.commit()

        result = app_module.run_sigenergy_sync(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)
        snapshot = conn.execute("SELECT * FROM integration_realtime_snapshots WHERE provider = 'Sigenergy'").fetchone()
        monitoring = conn.execute("SELECT * FROM monitoring_records WHERE source = 'Sigenergy'").fetchone()

    assert result["matched"] == 1
    assert result["snapshots"] == 1
    assert snapshot["asset_id"] == asset_id
    assert snapshot["grid_power_kw_raw"] == -0.5
    assert monitoring["asset_id"] == asset_id
    assert monitoring["status"] == "Operacional"


def test_run_sigenergy_sync_creates_unresolved_when_no_exact_match(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sigenergy-unresolved.db"
    app_module.ensure_database(str(db_path))
    monkeypatch.setattr(
        app_module,
        "run_provider_check",
        lambda _conn, _provider, dry_run=True: {
            "rows": [_sigenergy_sync_row("Unmapped Site")],
            "station_count": 1,
            "realtime_count": 1,
            "failed_realtime_count": 0,
        },
    )

    with get_db(str(db_path)) as conn:
        insert_enabled_sigenergy_config(conn)
        app_module.run_sigenergy_sync(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)
        unresolved = conn.execute("SELECT * FROM integration_unresolved WHERE provider = 'Sigenergy'").fetchone()
        monitoring_count = conn.execute("SELECT COUNT(*) AS total FROM monitoring_records WHERE source = 'Sigenergy'").fetchone()["total"]

    assert unresolved["external_id"] == "SIG-001"
    assert unresolved["external_name"] == "Unmapped Site"
    assert monitoring_count == 0


def test_parse_sigenergy_response_raises_sanitized_error_on_non_zero_provider_code() -> None:
    payload = load_fixture("error_code_payload.json")

    with pytest.raises(sigenergy_service.SigenergyAPIError, match=r"request limited.*code=42901"):
        sigenergy_service.parse_sigenergy_response(payload)


def test_validate_sigenergy_system_id_accepts_only_one_safe_id() -> None:
    assert app_module.validate_sigenergy_system_id(" SYS_001-AB ") == "SYS_001-AB"
    for value in ("", "SYS 001", "SYS,001", "SYS/001", "A" * 65):
        with pytest.raises(ValueError):
            app_module.validate_sigenergy_system_id(value)


def test_onboard_system_posts_list_payload_and_preserves_provider_code() -> None:
    calls: list[dict[str, Any]] = []

    class FakeSession:
        def post(self, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(load_fixture("auth_success_object.json"))

        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            calls.append({"method": method, "url": url, **kwargs})
            return FakeResponse({"code": 1401, "msg": "already exists"})

    result = sigenergy_service.onboard_system(sigenergy_config(), "SYS-001", session=FakeSession())

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://sigenergy.example.test/openapi/board/onboard"
    assert calls[0]["json"] == ["SYS-001"]
    assert result["status"] == "already_requested_or_onboarded"
    assert result["provider_code"] == "1401"


def test_sigenergy_client_refreshes_token_after_401() -> None:
    calls: list[str] = []

    class FakeSession:
        def post(self, url: str, **kwargs: Any) -> FakeResponse:
            calls.append("post")
            return FakeResponse(load_fixture("auth_success_object.json"))

        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            calls.append("request")
            if calls.count("request") == 1:
                return FakeResponse({}, status_code=401)
            return FakeResponse(load_fixture("systems_list.json"))

    rows = sigenergy_service.list_systems(sigenergy_config(), session=FakeSession())

    assert [row["systemId"] for row in rows] == ["SIG-001", "SIG-002"]
    assert calls == ["post", "request", "post", "request"]


def test_sanitize_sigenergy_error_redacts_token_and_auth_payload() -> None:
    message = 'Authorization: Bearer secret-token {"key":"base64secret"}'

    sanitized = sigenergy_service.sanitize_sigenergy_error(message)

    assert "secret-token" not in sanitized
    assert "base64secret" not in sanitized


def test_onboarding_duplicate_updates_existing_active_request(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-onboarding-duplicate.db"
    app_module.ensure_database(str(db_path))
    result = {
        "status": "requested",
        "provider_code": "0",
        "message": "ok",
        "response": {"code": 0, "msg": "ok"},
    }

    with get_db(str(db_path)) as conn:
        first_id = app_module.upsert_sigenergy_onboarding_request(conn, system_id="SYS-001", requested_by="u", result=result)
        second_id = app_module.upsert_sigenergy_onboarding_request(conn, system_id="sys-001", requested_by="u", result=result)
        row = conn.execute("SELECT attempt_count FROM sigenergy_onboarding_requests WHERE id = ?", (first_id,)).fetchone()

    assert first_id == second_id
    assert row["attempt_count"] == 2


def test_reconcile_sigenergy_onboarding_marks_pending_as_approved(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-onboarding-reconcile.db"
    app_module.ensure_database(str(db_path))

    with get_db(str(db_path)) as conn:
        app_module.upsert_sigenergy_onboarding_request(
            conn,
            system_id="SYS-001",
            requested_by="u",
            result={"status": "requested", "provider_code": "0", "message": "ok", "response": {}},
        )
        assert app_module.reconcile_sigenergy_onboarding_requests(conn, ["sys-001"]) == 1
        assert app_module.reconcile_sigenergy_onboarding_requests(conn, ["sys-001"]) == 0
        row = conn.execute("SELECT status, approved_at FROM sigenergy_onboarding_requests").fetchone()

    assert row["status"] == "approved"
    assert row["approved_at"]


def test_find_sigenergy_asset_id_ignores_inactive_alias(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-inactive-alias.db"
    app_module.ensure_database(str(db_path))

    with get_db(str(db_path)) as conn:
        asset_id = conn.execute("INSERT INTO assets (project_name, active_contract) VALUES ('Other', 'yes')").lastrowid
        conn.execute(
            "INSERT INTO asset_aliases (asset_id, alias_name, normalized_alias, active) VALUES (?, 'Sig Site', ?, 0)",
            (asset_id, app_module.normalize_name("Sig Site")),
        )
        conn.commit()

        assert app_module.find_sigenergy_asset_id(conn, "Sigenergy", "SIG-001", "Sig Site") is None


def test_cleanup_sigenergy_snapshots_keeps_latest_per_system(tmp_path) -> None:
    db_path = tmp_path / "sigenergy-retention.db"
    app_module.ensure_database(str(db_path))
    old = (app_module.datetime.now() - app_module.timedelta(days=120)).isoformat(timespec="seconds")
    new = app_module.datetime.now().isoformat(timespec="seconds")

    with get_db(str(db_path)) as conn:
        for external_id, collected_at in (("SIG-001", old), ("SIG-001", new), ("SIG-002", old)):
            conn.execute(
                """
                INSERT INTO integration_realtime_snapshots (provider, external_id, collected_at, payload_json)
                VALUES ('Sigenergy', ?, ?, '{}')
                """,
                (external_id, collected_at),
            )
        deleted = app_module.cleanup_sigenergy_snapshots(conn, "Sigenergy", 90)
        remaining = conn.execute("SELECT external_id, collected_at FROM integration_realtime_snapshots ORDER BY external_id, collected_at").fetchall()

    assert deleted == 1
    assert [(row["external_id"], row["collected_at"]) for row in remaining] == [("SIG-001", new), ("SIG-002", old)]


def test_run_integration_sync_persists_sigenergy_provider_failure(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sigenergy-sync-failure.db"
    app_module.ensure_database(str(db_path))
    error_payload = load_fixture("error_code_payload.json")
    provider_error = ValueError(f"{error_payload['msg']} (code={error_payload['code']})")

    def fake_provider_check(_conn, provider: str, dry_run: bool = False) -> dict[str, Any]:
        assert provider == app_module.INTEGRATION_PROVIDER_SIGENERGY
        assert dry_run is True
        raise provider_error

    monkeypatch.setattr(app_module, "run_provider_check", fake_provider_check)

    with get_db(str(db_path)) as conn:
        insert_enabled_sigenergy_config(conn)

        with pytest.raises(ValueError, match=r"request limited.*code=42901"):
            app_module.run_integration_sync(
                conn,
                app_module.INTEGRATION_PROVIDER_SIGENERGY,
                trigger_type="scheduled",
            )

        run = conn.execute(
            """
            SELECT status, error_message
            FROM integration_sync_runs
            WHERE provider = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (app_module.INTEGRATION_PROVIDER_SIGENERGY,),
        ).fetchone()
        config = conn.execute(
            """
            SELECT last_sync_status, last_error
            FROM integration_configs
            WHERE provider = ?
            """,
            (app_module.INTEGRATION_PROVIDER_SIGENERGY,),
        ).fetchone()

    assert run["status"] == "error"
    assert "request limited for fake test account" in run["error_message"]
    assert "code=42901" in run["error_message"]
    assert config["last_sync_status"] == "error"
    assert "request limited for fake test account" in config["last_error"]
