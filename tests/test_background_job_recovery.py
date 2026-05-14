from __future__ import annotations

from datetime import datetime

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
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO background_jobs (job_type, status, params_json, created_at, started_at)
        VALUES (?, ?, '{}', ?, ?)
        """,
        (job_type, status, created_at, started_at),
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
        schedule_pending_background_jobs(make_test_app(db_path))
    finally:
        app_module.app.config["DATABASE"] = previous_db

    assert scheduled_ids == pending_ids
    assert len(scheduled_ids) == 12
    assert len(scheduled_ids) == len(set(scheduled_ids))
