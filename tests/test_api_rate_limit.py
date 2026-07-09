from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import requests

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.services.api_rate_limit import ApiRateLimitError, mark_api_cooldown


class HttpResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            exc = requests.HTTPError(f"{self.status_code} error")
            exc.response = self
            raise exc

    def json(self) -> dict[str, Any]:
        return self.payload


def test_fusionsolar_http_500_retries_with_short_backoff_then_succeeds(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "fusion-500.db"
    app_module.ensure_database(str(db_path))
    flask_app = app_module.app
    original_database = flask_app.config["DATABASE"]
    sleeps: list[float] = []

    class FlakySession:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, url: str, json: dict[str, Any], timeout: int) -> HttpResponse:
            self.calls += 1
            if self.calls < 3:
                return HttpResponse({}, status_code=500)
            return HttpResponse({"success": True, "failCode": 0, "data": []})

    session = FlakySession()
    monkeypatch.setattr(app_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    try:
        flask_app.config["DATABASE"] = str(db_path)
        with flask_app.app_context():
            result = app_module.post_fusionsolar_json(
                session,
                "https://fusion.test/thirdData/getStationRealKpi",
                {"stationCodes": "S1"},
                expected_message="failed",
            )
    finally:
        flask_app.config["DATABASE"] = original_database

    assert result["success"] is True
    assert session.calls == 3
    assert sleeps == [15, 60]


def test_background_job_waits_rate_limit_and_reschedules(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "waiting-job.db"
    app_module.ensure_database(str(db_path))
    flask_app = app_module.app
    original_database = flask_app.config["DATABASE"]
    scheduled: list[tuple[int, datetime | None]] = []
    cooldown_until = datetime.now() + timedelta(minutes=60)

    def fake_sync(_conn, _provider: str, trigger_type: str = "manual") -> dict[str, Any]:
        raise ApiRateLimitError(
            app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
            app_module.API_AREA_STATE,
            cooldown_until,
            "FusionSolar temporariamente limitado pela API.",
        )

    monkeypatch.setattr(app_module, "run_fusionsolar_sync", fake_sync)
    monkeypatch.setattr(app_module, "schedule_background_job", lambda _app, job_id, run_date=None: scheduled.append((job_id, run_date)) or True)
    try:
        flask_app.config["DATABASE"] = str(db_path)
        with get_db(str(db_path)) as conn:
            job_id, _ = app_module.create_background_job(
                conn,
                "fusionsolar_state_sync",
                {"provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR},
            )
            conn.commit()

        app_module.run_background_job(flask_app, job_id)

        with get_db(str(db_path)) as conn:
            job = conn.execute("SELECT status, next_attempt_at, error_message FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        flask_app.config["DATABASE"] = original_database

    assert job["status"] == "waiting_rate_limit"
    assert job["next_attempt_at"] == cooldown_until.isoformat(timespec="seconds")
    assert "limitado" in job["error_message"]
    assert scheduled == [(job_id, cooldown_until)]


def test_sync_during_active_cooldown_does_not_call_api(tmp_path) -> None:
    db_path = tmp_path / "active-cooldown.db"
    app_module.ensure_database(str(db_path))
    called = False
    with get_db(str(db_path)) as conn:
        mark_api_cooldown(
            conn,
            app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
            app_module.API_AREA_STATE,
            "limited",
            cooldown_until=datetime.now() + timedelta(minutes=30),
        )
        conn.commit()

        def fake_sync(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal called
            called = True
            return {}

        original = app_module.run_fusionsolar_sync
        app_module.run_fusionsolar_sync = fake_sync
        try:
            with pytest.raises(ApiRateLimitError):
                app_module.run_background_job_payload(
                    conn,
                    "fusionsolar_state_sync",
                    {"provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR},
                )
        finally:
            app_module.run_fusionsolar_sync = original

    assert called is False


def test_job_runs_after_cooldown_expires(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "expired-cooldown.db"
    app_module.ensure_database(str(db_path))
    calls: list[str] = []

    def fake_sync(_conn, provider: str, trigger_type: str = "manual") -> dict[str, Any]:
        calls.append(provider)
        return {"matched": 1, "unresolved": 0, "auto_resolved": 0}

    monkeypatch.setattr(app_module, "run_fusionsolar_sync", fake_sync)
    with get_db(str(db_path)) as conn:
        mark_api_cooldown(
            conn,
            app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
            app_module.API_AREA_STATE,
            "old limited",
            cooldown_until=datetime.now() - timedelta(minutes=1),
        )
        conn.commit()
        result = app_module.run_background_job_payload(
            conn,
            "fusionsolar_state_sync",
            {"provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR},
        )

    assert result["matched"] == 1
    assert calls == [app_module.INTEGRATION_PROVIDER_FUSIONSOLAR]


def test_fusionsolar_305_relogs_once(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "fusion-305.db"
    app_module.ensure_database(str(db_path))
    get_session_calls: list[bool] = []
    station_calls = 0

    def fake_session(_config: dict[str, Any], *, force_login: bool = False):
        get_session_calls.append(force_login)
        return object(), "token"

    def fake_stations(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal station_calls
        station_calls += 1
        if station_calls == 1:
            raise app_module.FusionSolarApiError(
                "USER_MUST_RELOGIN (failCode=305)",
                payload={"success": False, "failCode": 305, "message": "USER_MUST_RELOGIN"},
            )
        return [{"plantCode": "S1", "plantName": "Plant"}]

    monkeypatch.setattr(app_module, "get_fusionsolar_session", fake_session)
    monkeypatch.setattr(app_module, "fetch_fusionsolar_stations", fake_stations)
    monkeypatch.setattr(app_module, "fetch_fusionsolar_realtime_map", lambda *_args, **_kwargs: {"S1": {}})

    with get_db(str(db_path)) as conn:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT OR REPLACE INTO integration_configs (
                provider, enabled, username, password, base_url, created_at, updated_at
            ) VALUES ('FusionSolar', 1, 'u', 'p', 'https://fusion.test', ?, ?)
            """,
            (now, now),
        )
        conn.commit()
        result = app_module.run_fusionsolar_check(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR, dry_run=True, include_diagnostics=False)

    assert result["station_count"] == 1
    assert get_session_calls == [False, True]


def test_sigenergy_429_marks_persistent_cooldown(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sigenergy-429.db"
    app_module.ensure_database(str(db_path))

    def fake_provider_check(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ApiRateLimitError(
            app_module.INTEGRATION_PROVIDER_SIGENERGY,
            app_module.API_AREA_STATE,
            datetime.now() + timedelta(minutes=60),
            "Sigenergy HTTP 429",
        )

    monkeypatch.setattr(app_module, "run_provider_check", fake_provider_check)
    with get_db(str(db_path)) as conn:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT OR REPLACE INTO integration_configs (
                provider, enabled, username, password, base_url, created_at, updated_at
            ) VALUES ('Sigenergy', 1, 'u', 'p', 'https://sig.test', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

        with pytest.raises(ApiRateLimitError):
            app_module.run_sigenergy_sync(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)

        state = app_module.get_api_call_state(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY, app_module.API_AREA_STATE)

    assert state["cooldown_until"]
    assert "429" in state["last_error"]
