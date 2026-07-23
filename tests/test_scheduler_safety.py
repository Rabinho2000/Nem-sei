from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import app as app_module
import pytest
from monitoring_board.db import get_db


@dataclass
class FakeJob:
    id: str


class FakeScheduler:
    def __init__(self) -> None:
        self.started = 0
        self.jobs: dict[str, FakeJob] = {}
        self.add_calls: list[dict[str, Any]] = []
        self.remove_calls: list[str] = []

    def start(self) -> None:
        self.started += 1

    def get_jobs(self) -> list[FakeJob]:
        return list(self.jobs.values())

    def remove_job(self, job_id: str) -> None:
        self.remove_calls.append(job_id)
        self.jobs.pop(job_id, None)

    def add_job(self, **kwargs: Any) -> None:
        self.add_calls.append(kwargs)
        self.jobs[str(kwargs["id"])] = FakeJob(str(kwargs["id"]))


def _insert_enabled_config(
    conn,
    provider: str,
    sync_hours: str = "08:00,14:00",
    *,
    enabled: int = 1,
    auto_sync_enabled: int = 1,
    production_sync_enabled: int = 1,
    diagnostics_sync_enabled: int = 1,
) -> None:
    now = app_module.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR REPLACE INTO integration_configs (
            provider, enabled, auto_sync_enabled, sync_hours,
            production_sync_enabled, diagnostics_sync_enabled, state_sync_interval_hours,
            production_sync_time, diagnostics_sync_time, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, '00:10', '00:30', ?, ?)
        """,
        (
            provider,
            enabled,
            auto_sync_enabled,
            sync_hours,
            production_sync_enabled,
            diagnostics_sync_enabled,
            now,
            now,
        ),
    )
    conn.commit()


def _make_test_app(tmp_path):
    db_path = tmp_path / "scheduler-safety.db"
    app_module.ensure_database(str(db_path))
    flask_app = app_module.app
    original_database = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    return flask_app, original_database


def test_refresh_scheduler_registers_stable_single_instance_provider_jobs(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    fake_scheduler = FakeScheduler()
    fake_scheduler.jobs["fusionsolar-sync-1"] = FakeJob("fusionsolar-sync-1")
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: True)
    app_module.SCHEDULER = fake_scheduler
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)

        app_module.refresh_integration_scheduler(flask_app)
        first_ids = sorted(fake_scheduler.jobs)
        app_module.refresh_integration_scheduler(flask_app)
        second_ids = sorted(fake_scheduler.jobs)

        assert first_ids == second_ids
        assert len(second_ids) == len(set(second_ids))
        assert "fusionsolar-sync-1" not in second_ids
        assert "fusionsolar-sync-1" in fake_scheduler.remove_calls
        assert "integration-state-fusionsolar-hourly" in second_ids
        assert "integration-production-fusionsolar-daily" in second_ids
        assert "integration-diagnostics-fusionsolar-daily" in second_ids
        assert "integration-production-fusionsolar-month-close" in second_ids
        assert "integration-state-sigenergy-hourly" in second_ids

        recurring_calls = [
            call
            for call in fake_scheduler.add_calls
            if str(call["id"]).startswith("integration-")
            or call["id"] == "telegram-daily-summary"
        ]
        assert recurring_calls
        for call in recurring_calls:
            assert call["replace_existing"] is True
            assert call["max_instances"] == 1
            assert call["coalesce"] is True
            assert call["misfire_grace_time"] == 1800

        fusionsolar_state_call = next(call for call in fake_scheduler.add_calls if call["id"] == "integration-state-fusionsolar-hourly")
        assert fusionsolar_state_call["trigger"] == "interval"
        assert fusionsolar_state_call["hours"] == 1

        production_call = next(call for call in fake_scheduler.add_calls if call["id"] == "integration-production-fusionsolar-daily")
        assert production_call["trigger"] == "cron"
        assert production_call["hour"] == 0
        assert production_call["minute"] == 10

        wat_call = next(call for call in fake_scheduler.add_calls if call["id"] == "integration-diagnostics-fusionsolar-daily")
        assert wat_call["trigger"] == "cron"
        assert wat_call["hour"] == 0
        assert wat_call["minute"] == 30

        month_close_call = next(
            call for call in fake_scheduler.add_calls
            if call["id"] == "integration-production-fusionsolar-month-close"
        )
        assert month_close_call["trigger"] == "cron"
        assert month_close_call["day"] == "1-5"
        assert month_close_call["hour"] == 2
        assert month_close_call["minute"] == 0
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_fusionsolar_disabled_registers_no_provider_jobs(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    fake_scheduler = FakeScheduler()
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: False)
    app_module.SCHEDULER = fake_scheduler
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(
                conn,
                app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                enabled=0,
                auto_sync_enabled=1,
                production_sync_enabled=1,
                diagnostics_sync_enabled=1,
            )

        app_module.refresh_integration_scheduler(flask_app)

        assert "integration-state-fusionsolar-hourly" not in fake_scheduler.jobs
        assert "integration-production-fusionsolar-daily" not in fake_scheduler.jobs
        assert "integration-diagnostics-fusionsolar-daily" not in fake_scheduler.jobs
        assert "integration-production-fusionsolar-month-close" not in fake_scheduler.jobs
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_fusionsolar_enabled_with_auto_sync_disabled_does_not_register_hourly_state_job(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    fake_scheduler = FakeScheduler()
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: False)
    app_module.SCHEDULER = fake_scheduler
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(
                conn,
                app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                auto_sync_enabled=0,
            )

        app_module.refresh_integration_scheduler(flask_app)

        assert "integration-state-fusionsolar-hourly" not in fake_scheduler.jobs
        assert "integration-production-fusionsolar-daily" in fake_scheduler.jobs
        assert "integration-diagnostics-fusionsolar-daily" in fake_scheduler.jobs
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_fusionsolar_enabled_with_auto_sync_enabled_registers_single_hourly_state_job(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    fake_scheduler = FakeScheduler()
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: False)
    app_module.SCHEDULER = fake_scheduler
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        app_module.refresh_integration_scheduler(flask_app)

        state_jobs = [job_id for job_id in fake_scheduler.jobs if job_id == "integration-state-fusionsolar-hourly"]
        assert state_jobs == ["integration-state-fusionsolar-hourly"]
        assert not any(str(job_id).startswith("integration-sync-fusionsolar-") for job_id in fake_scheduler.jobs)
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_fusionsolar_production_sync_flag_controls_daily_job(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: False)
    try:
        fake_scheduler = FakeScheduler()
        app_module.SCHEDULER = fake_scheduler
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(
                conn,
                app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                production_sync_enabled=1,
                diagnostics_sync_enabled=0,
            )
        app_module.refresh_integration_scheduler(flask_app)
        assert "integration-production-fusionsolar-daily" in fake_scheduler.jobs
        assert "integration-production-fusionsolar-month-close" in fake_scheduler.jobs

        fake_scheduler = FakeScheduler()
        app_module.SCHEDULER = fake_scheduler
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(
                conn,
                app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                production_sync_enabled=0,
                diagnostics_sync_enabled=0,
            )
        app_module.refresh_integration_scheduler(flask_app)
        assert "integration-production-fusionsolar-daily" not in fake_scheduler.jobs
        assert "integration-production-fusionsolar-month-close" not in fake_scheduler.jobs
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_fusionsolar_diagnostics_sync_flag_controls_daily_job(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: False)
    try:
        fake_scheduler = FakeScheduler()
        app_module.SCHEDULER = fake_scheduler
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(
                conn,
                app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                production_sync_enabled=0,
                diagnostics_sync_enabled=1,
            )
        app_module.refresh_integration_scheduler(flask_app)
        assert "integration-diagnostics-fusionsolar-daily" in fake_scheduler.jobs

        fake_scheduler = FakeScheduler()
        app_module.SCHEDULER = fake_scheduler
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(
                conn,
                app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                production_sync_enabled=0,
                diagnostics_sync_enabled=0,
            )
        app_module.refresh_integration_scheduler(flask_app)
        assert "integration-diagnostics-fusionsolar-daily" not in fake_scheduler.jobs
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_sigenergy_registers_only_hourly_state_job(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    fake_scheduler = FakeScheduler()
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: False)
    app_module.SCHEDULER = fake_scheduler
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)

        app_module.refresh_integration_scheduler(flask_app)

        assert sorted(fake_scheduler.jobs) == ["background-jobs-reactivate-rate-limit", "integration-state-sigenergy-hourly"]
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_sigenergy_auto_sync_disabled_registers_no_state_job(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    fake_scheduler = FakeScheduler()
    original_scheduler = app_module.SCHEDULER
    monkeypatch.setattr(app_module, "telegram_daily_summary_enabled", lambda: False)
    app_module.SCHEDULER = fake_scheduler
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(
                conn,
                app_module.INTEGRATION_PROVIDER_SIGENERGY,
                auto_sync_enabled=0,
            )

        app_module.refresh_integration_scheduler(flask_app)

        assert sorted(fake_scheduler.jobs) == ["background-jobs-reactivate-rate-limit"]
    finally:
        flask_app.config["DATABASE"] = original_database
        app_module.SCHEDULER = original_scheduler


def test_scheduled_sigenergy_sync_uses_provider_neutral_wrapper(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_run_integration_sync(_conn, provider: str, trigger_type: str = "manual") -> dict[str, int]:
        calls.append((provider, trigger_type))
        return {"matched": 0}

    monkeypatch.setattr(app_module, "run_integration_sync", fake_run_integration_sync)
    try:
        app_module.run_scheduled_integration_sync(flask_app, app_module.INTEGRATION_PROVIDER_SIGENERGY)
    finally:
        flask_app.config["DATABASE"] = original_database

    assert calls == [(app_module.INTEGRATION_PROVIDER_SIGENERGY, "scheduled")]


def test_scheduled_fusionsolar_wat_queues_previous_day(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    scheduled: list[int] = []
    monkeypatch.setattr(app_module, "current_lisbon_date", lambda: app_module.date(2026, 6, 15))
    monkeypatch.setattr(app_module, "schedule_background_job", lambda _app, job_id: scheduled.append(job_id) or True)
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        app_module.run_scheduled_fusionsolar_wat_backfill(flask_app)

        with get_db(flask_app.config["DATABASE"]) as conn:
            job = conn.execute(
                "SELECT id, job_type, status, params_json FROM background_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
    finally:
        flask_app.config["DATABASE"] = original_database

    params = json.loads(job["params_json"])
    expected_date = "2026-06-14"
    assert job["job_type"] == "fusionsolar_inverter_availability_backfill"
    assert job["status"] == "pending"
    assert params == {
        "api_area": app_module.API_AREA_DIAGNOSTICS,
        "from_date": expected_date,
        "provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
        "to_date": expected_date,
        "trigger_type": "scheduled",
    }
    assert scheduled == [job["id"]]


def test_scheduled_fusionsolar_production_queues_previous_day(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    scheduled: list[int] = []
    monkeypatch.setattr(app_module, "current_lisbon_date", lambda: app_module.date(2026, 6, 15))
    monkeypatch.setattr(app_module, "schedule_background_job", lambda _app, job_id: scheduled.append(job_id) or True)
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        app_module.run_scheduled_fusionsolar_production_sync(flask_app)

        with get_db(flask_app.config["DATABASE"]) as conn:
            job = conn.execute(
                "SELECT id, job_type, status, params_json FROM background_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
    finally:
        flask_app.config["DATABASE"] = original_database

    params = json.loads(job["params_json"])
    expected_date = "2026-06-14"
    assert job["job_type"] == "fusionsolar_production_sync"
    assert job["status"] == "pending"
    assert params == {
        "api_area": app_module.API_AREA_PRODUCTION,
        "period_type": "day",
        "provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
        "target_date": expected_date,
        "trigger_type": "scheduled",
    }
    assert scheduled == [job["id"]]


@pytest.mark.parametrize(
    ("scheduler_date", "expected_month"),
    [
        (app_module.date(2026, 6, 1), "2026-05"),
        (app_module.date(2026, 6, 5), "2026-05"),
        (app_module.date(2026, 1, 3), "2025-12"),
    ],
)
def test_scheduled_fusionsolar_month_close_queues_previous_month_on_days_1_to_5(
    tmp_path,
    monkeypatch,
    scheduler_date,
    expected_month,
) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    scheduled: list[int] = []
    monkeypatch.setattr(app_module, "current_lisbon_date", lambda: scheduler_date)
    monkeypatch.setattr(app_module, "schedule_background_job", lambda _app, job_id: scheduled.append(job_id) or True)
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        app_module.run_scheduled_fusionsolar_month_close(flask_app)

        with get_db(flask_app.config["DATABASE"]) as conn:
            job = conn.execute(
                "SELECT id, job_type, status, params_json FROM background_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
    finally:
        flask_app.config["DATABASE"] = original_database

    params = json.loads(job["params_json"])
    assert job["job_type"] == "fusionsolar_month_close"
    assert job["status"] == "pending"
    assert params["report_month"] == expected_month
    assert params["report_month"] < scheduler_date.strftime("%Y-%m")
    assert params["trigger_type"] == "scheduled_month_close"
    assert scheduled == [job["id"]]


def test_scheduled_fusionsolar_month_close_does_not_queue_outside_days_1_to_5(
    tmp_path,
    monkeypatch,
) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    monkeypatch.setattr(app_module, "current_lisbon_date", lambda: app_module.date(2026, 6, 6))
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        app_module.run_scheduled_fusionsolar_month_close(flask_app)

        with get_db(flask_app.config["DATABASE"]) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM background_jobs WHERE job_type = 'fusionsolar_month_close'"
            ).fetchone()[0]
    finally:
        flask_app.config["DATABASE"] = original_database

    assert count == 0


def test_scheduled_fusionsolar_wat_reuses_pending_job(tmp_path, monkeypatch) -> None:
    flask_app, original_database = _make_test_app(tmp_path)
    scheduled: list[int] = []
    monkeypatch.setattr(app_module, "current_lisbon_date", lambda: app_module.date(2026, 6, 15))
    monkeypatch.setattr(app_module, "schedule_background_job", lambda _app, job_id: scheduled.append(job_id) or True)
    try:
        with get_db(flask_app.config["DATABASE"]) as conn:
            _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        app_module.run_scheduled_fusionsolar_wat_backfill(flask_app)
        app_module.run_scheduled_fusionsolar_wat_backfill(flask_app)

        with get_db(flask_app.config["DATABASE"]) as conn:
            jobs = conn.execute(
                "SELECT id FROM background_jobs WHERE job_type = ?",
                ("fusionsolar_inverter_availability_backfill",),
            ).fetchall()
    finally:
        flask_app.config["DATABASE"] = original_database

    assert len(jobs) == 1
    assert scheduled == [jobs[0]["id"]]


def test_run_all_integration_syncs_dispatches_enabled_providers_through_wrapper(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "all-syncs.db"
    app_module.ensure_database(str(db_path))
    calls: list[tuple[str, str]] = []

    def fake_run_integration_sync(_conn, provider: str, trigger_type: str = "manual") -> dict[str, int]:
        calls.append((provider, trigger_type))
        return {"matched": 1}

    monkeypatch.setattr(app_module, "run_integration_sync", fake_run_integration_sync)
    with get_db(str(db_path)) as conn:
        _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)
        _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)

        result = app_module.run_all_integration_syncs(conn, trigger_type="manual-test")

    assert calls == [
        (app_module.INTEGRATION_PROVIDER_FUSIONSOLAR, "manual-test"),
        (app_module.INTEGRATION_PROVIDER_SIGENERGY, "manual-test"),
    ]
    assert sorted(result["results"]) == [
        app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
        app_module.INTEGRATION_PROVIDER_SIGENERGY,
    ]


def test_provider_sync_failures_persist_through_existing_sync_run_path(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sync-failure.db"
    app_module.ensure_database(str(db_path))

    def fake_provider_check(_conn, _provider: str, dry_run: bool = False) -> dict[str, Any]:
        raise ValueError("provider unavailable")

    if hasattr(app_module, "run_provider_check"):
        monkeypatch.setattr(app_module, "run_provider_check", fake_provider_check)
    else:
        monkeypatch.setattr(app_module, "run_fusionsolar_check", fake_provider_check)
    with get_db(str(db_path)) as conn:
        _insert_enabled_config(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)

        try:
            app_module.run_integration_sync(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY, trigger_type="scheduled")
        except ValueError:
            pass

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
            "SELECT last_sync_status, last_error FROM integration_configs WHERE provider = ?",
            (app_module.INTEGRATION_PROVIDER_SIGENERGY,),
        ).fetchone()

    assert run["status"] == "error"
    assert "provider unavailable" in run["error_message"]
    assert config["last_sync_status"] == "error"
    assert "provider unavailable" in config["last_error"]
