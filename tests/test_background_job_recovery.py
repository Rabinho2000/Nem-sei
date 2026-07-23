from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import app as app_module
from app import ensure_database, schedule_pending_background_jobs
from monitoring_board.db import get_db


def insert_background_job(
    conn,
    *,
    job_type: str = "fusionsolar_production_sync",
    status: str = "pending",
    created_at: str = "2026-05-14T09:00:00",
    started_at: str | None = None,
    next_attempt_at: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO background_jobs (job_type, status, params_json, created_at, started_at, next_attempt_at)
        VALUES (?, ?, '{}', ?, ?, ?)
        """,
        (job_type, status, created_at, started_at, next_attempt_at),
    )
    return int(cursor.lastrowid)


def make_test_app(db_path):
    test_app = app_module.app
    test_app.config["DATABASE"] = str(db_path)
    return test_app


def test_pending_background_job_recovery_schedules_every_pending_job_in_id_order(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "background-jobs.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        pending_ids = [
            insert_background_job(conn, created_at=f"2026-05-14T09:{minute:02d}:00")
            for minute in range(12)
        ]
        insert_background_job(conn, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
        insert_background_job(conn, status="success")
        insert_background_job(conn, status="failed")
        conn.commit()
    finally:
        conn.close()

    scheduled_ids: list[int] = []

    def fake_schedule_background_job(_app, job_id: int) -> bool:
        scheduled_ids.append(job_id)
        return True

    monkeypatch.setattr(app_module, "schedule_background_job", fake_schedule_background_job)

    previous_db = app_module.app.config["DATABASE"]
    try:
        summary = schedule_pending_background_jobs(make_test_app(db_path))
    finally:
        app_module.app.config["DATABASE"] = previous_db

    assert scheduled_ids == pending_ids
    assert len(scheduled_ids) == 12
    assert len(scheduled_ids) == len(set(scheduled_ids))
    assert summary == {
        "stale_running_failed": 0,
        "rate_limit_reactivated": 0,
        "pending_found": 12,
        "pending_scheduled": 12,
        "pending_schedule_failed_ids": [],
        "waiting_found": 0,
        "waiting_scheduled": 0,
        "waiting_schedule_failed_ids": [],
    }


def test_background_job_recovery_reports_stale_and_failed_scheduling_without_losing_pending_work(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "background-jobs.db"
    ensure_database(str(db_path))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_started_at = (now - timedelta(minutes=45)).isoformat(timespec="seconds")
    fresh_started_at = now.isoformat(timespec="seconds")
    conn = get_db(str(db_path))
    try:
        stale_id = insert_background_job(conn, status="running", started_at=stale_started_at)
        fresh_id = insert_background_job(conn, status="running", started_at=fresh_started_at)
        successful_pending_id = insert_background_job(conn)
        failed_pending_id = insert_background_job(conn)
        conn.commit()
    finally:
        conn.close()

    scheduled_ids: list[int] = []

    def fake_schedule_background_job(_app, job_id: int) -> bool:
        scheduled_ids.append(job_id)
        return job_id != failed_pending_id

    monkeypatch.setattr(app_module, "schedule_background_job", fake_schedule_background_job)

    previous_db = app_module.app.config["DATABASE"]
    try:
        summary = schedule_pending_background_jobs(make_test_app(db_path))
    finally:
        app_module.app.config["DATABASE"] = previous_db

    conn = get_db(str(db_path))
    try:
        stale_job = conn.execute(
            "SELECT status, error_message, finished_at FROM background_jobs WHERE id = ?",
            (stale_id,),
        ).fetchone()
        fresh_job = conn.execute(
            "SELECT status, error_message, finished_at FROM background_jobs WHERE id = ?",
            (fresh_id,),
        ).fetchone()
        failed_pending_job = conn.execute(
            "SELECT status, error_message, finished_at FROM background_jobs WHERE id = ?",
            (failed_pending_id,),
        ).fetchone()
    finally:
        conn.close()

    assert scheduled_ids == [successful_pending_id, failed_pending_id]
    assert summary == {
        "stale_running_failed": 1,
        "rate_limit_reactivated": 0,
        "pending_found": 2,
        "pending_scheduled": 1,
        "pending_schedule_failed_ids": [failed_pending_id],
        "waiting_found": 0,
        "waiting_scheduled": 0,
        "waiting_schedule_failed_ids": [],
    }
    assert stale_job["status"] == "failed"
    assert "running for more than 30 minutes" in stale_job["error_message"]
    assert stale_job["finished_at"]
    assert fresh_job["status"] == "running"
    assert fresh_job["finished_at"] is None
    assert failed_pending_job["status"] == "pending"
    assert failed_pending_job["error_message"] is None
    assert failed_pending_job["finished_at"] is None


def test_due_rate_limited_background_jobs_are_reactivated_and_scheduled(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "background-rate-limit.db"
    ensure_database(str(db_path))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    due = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (now + timedelta(minutes=10)).isoformat(timespec="seconds")
    conn = get_db(str(db_path))
    try:
        due_id = insert_background_job(conn, status="waiting_rate_limit", next_attempt_at=due)
        future_id = insert_background_job(conn, status="waiting_rate_limit", next_attempt_at=future)
        conn.commit()
    finally:
        conn.close()

    scheduled: list[tuple[int, datetime | None]] = []
    monkeypatch.setattr(
        app_module,
        "schedule_background_job",
        lambda _app, job_id, run_date=None: scheduled.append(
            (job_id, run_date)
        )
        or True,
    )

    previous_db = app_module.app.config["DATABASE"]
    try:
        summary = schedule_pending_background_jobs(make_test_app(db_path))
    finally:
        app_module.app.config["DATABASE"] = previous_db

    conn = get_db(str(db_path))
    try:
        due_job = conn.execute("SELECT status, next_attempt_at FROM background_jobs WHERE id = ?", (due_id,)).fetchone()
        future_job = conn.execute("SELECT status, next_attempt_at FROM background_jobs WHERE id = ?", (future_id,)).fetchone()
    finally:
        conn.close()

    assert [job_id for job_id, _run_date in scheduled] == [due_id, future_id]
    assert scheduled[0][1] is None
    assert scheduled[1][1] == datetime.fromisoformat(future).replace(
        tzinfo=timezone.utc
    )
    assert summary["rate_limit_reactivated"] == 1
    assert summary["waiting_scheduled"] == 1
    assert due_job["status"] == "pending"
    assert due_job["next_attempt_at"] == due
    assert future_job["status"] == "waiting_rate_limit"


def test_create_background_job_reuses_existing_job_with_same_type_and_params(tmp_path) -> None:
    db_path = tmp_path / "background-dedupe.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        first_id, first_created = app_module.create_background_job(
            conn,
            "fusionsolar_state_sync",
            {"provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR, "trigger_type": "manual_background"},
        )
        second_id, second_created = app_module.create_background_job(
            conn,
            "fusionsolar_state_sync",
            {"provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR, "trigger_type": "manual_background"},
        )
        job_count = conn.execute("SELECT COUNT(*) AS total FROM background_jobs").fetchone()["total"]
        params = conn.execute("SELECT params_json FROM background_jobs WHERE id = ?", (first_id,)).fetchone()["params_json"]

    decoded = json.loads(params)
    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert job_count == 1
    assert decoded["provider"] == app_module.INTEGRATION_PROVIDER_FUSIONSOLAR
    assert decoded["api_area"] == app_module.API_AREA_STATE


def test_create_background_job_allows_same_type_with_different_params(tmp_path) -> None:
    db_path = tmp_path / "background-dedupe-params.db"
    ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        first_id, first_created = app_module.create_background_job(
            conn,
            "fusionsolar_production_sync",
            {
                "provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                "target_date": "2026-06-14",
                "period_type": "day",
                "trigger_type": "manual_background",
            },
        )
        second_id, second_created = app_module.create_background_job(
            conn,
            "fusionsolar_production_sync",
            {
                "provider": app_module.INTEGRATION_PROVIDER_FUSIONSOLAR,
                "target_date": "2026-06-15",
                "period_type": "day",
                "trigger_type": "manual_background",
            },
        )
        job_count = conn.execute("SELECT COUNT(*) AS total FROM background_jobs").fetchone()["total"]

    assert first_created is True
    assert second_created is True
    assert second_id != first_id
    assert job_count == 2
