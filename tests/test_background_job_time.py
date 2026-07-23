from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import app as app_module
import pytest
from app import ensure_database
from monitoring_board.db import get_db
from monitoring_board.services.production_api_queue import (
    ApiQueuePolicy,
    PRODUCTION_KPI_AREA,
    release_api_lease,
    reserve_api_slot,
)


LISBON = ZoneInfo("Europe/Lisbon")
UTC = timezone.utc


@pytest.mark.parametrize(
    "started_at",
    (
        datetime(2026, 7, 23, 2, 0, tzinfo=LISBON),
        datetime(2026, 1, 23, 2, 0, tzinfo=LISBON),
    ),
)
def test_waiting_api_slot_resumes_after_65_seconds_in_summer_and_winter(
    tmp_path,
    monkeypatch,
    started_at: datetime,
) -> None:
    db_path = tmp_path / f"slot-{started_at.month}.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    job_id, _created = app_module.create_background_job(
        conn,
        "fusionsolar_production_sync",
        {"target_date": "2026-01-22"},
    )
    conn.commit()
    resume_at = started_at + timedelta(seconds=65)
    app_module.mark_background_job_waiting_api_slot(
        conn,
        job_id,
        next_attempt_at=resume_at,
        wait_reason="min_interval",
        error_message="waiting",
    )

    row = conn.execute(
        "SELECT next_attempt_at FROM background_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["next_attempt_at"] == resume_at.astimezone(UTC).isoformat(
        timespec="seconds"
    )

    monkeypatch.setattr(
        app_module,
        "background_job_utc_now",
        lambda: resume_at.astimezone(UTC) - timedelta(seconds=1),
    )
    assert app_module.mark_background_job_running(conn, job_id) is False

    monkeypatch.setattr(
        app_module,
        "background_job_utc_now",
        lambda: resume_at.astimezone(UTC),
    )
    assert app_module.mark_background_job_running(conn, job_id) is True
    conn.close()


@pytest.mark.parametrize(
    ("now_lisbon", "expected_midnight_utc"),
    (
        (
            datetime(2026, 7, 23, 12, 0, tzinfo=LISBON),
            datetime(2026, 7, 23, 23, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 23, 12, 0, tzinfo=LISBON),
            datetime(2026, 1, 24, 0, 0, tzinfo=UTC),
        ),
    ),
)
def test_daily_budget_midnight_is_persisted_and_scheduled_in_utc(
    tmp_path,
    monkeypatch,
    now_lisbon: datetime,
    expected_midnight_utc: datetime,
) -> None:
    db_path = tmp_path / f"budget-{now_lisbon.month}.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    policy = ApiQueuePolicy(
        min_interval_seconds=0,
        daily_budget=1,
        lease_seconds=30,
    )
    first = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="first",
        priority=1,
        policy=policy,
        now=now_lisbon,
    )
    assert first.granted is True
    release_api_lease(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="first",
        now=now_lisbon,
    )
    blocked = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="second",
        priority=1,
        policy=policy,
        now=now_lisbon + timedelta(seconds=1),
    )
    assert blocked.wait_reason == "daily_budget"
    assert blocked.next_attempt_at.astimezone(UTC) == expected_midnight_utc

    job_id, _created = app_module.create_background_job(
        conn,
        "fusionsolar_production_sync",
        {"target_date": "2026-01-22"},
    )
    conn.commit()
    app_module.mark_background_job_waiting_api_slot(
        conn,
        job_id,
        next_attempt_at=blocked.next_attempt_at,
        wait_reason=blocked.wait_reason,
        error_message="daily budget",
    )
    stored = conn.execute(
        "SELECT next_attempt_at FROM background_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()["next_attempt_at"]
    assert stored == expected_midnight_utc.isoformat(timespec="seconds")

    captured: dict[str, object] = {}

    class Scheduler:
        def add_job(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(app_module, "SCHEDULER", Scheduler())
    assert app_module.schedule_background_job(
        app_module.app,
        job_id,
        run_date=blocked.next_attempt_at,
    )
    assert captured["run_date"] == expected_midnight_utc
    assert captured["run_date"].tzinfo is UTC
    conn.close()


def test_407_waiting_job_is_restored_at_the_same_instant_after_restart(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "restart-407.db"
    ensure_database(str(db_path))
    resume_lisbon = datetime(2026, 7, 23, 4, 0, tzinfo=LISBON)
    before_resume = resume_lisbon.astimezone(UTC) - timedelta(minutes=10)
    conn = get_db(str(db_path))
    job_id, _created = app_module.create_background_job(
        conn,
        "fusionsolar_production_sync",
        {"target_date": "2026-07-22"},
    )
    conn.commit()
    app_module.mark_background_job_waiting_rate_limit(
        conn,
        job_id,
        next_attempt_at=resume_lisbon,
        error_message="407",
    )
    conn.close()

    scheduled: list[tuple[int, datetime | None]] = []
    monkeypatch.setattr(
        app_module,
        "background_job_utc_now",
        lambda: before_resume,
    )
    monkeypatch.setattr(
        app_module,
        "schedule_background_job",
        lambda _app, scheduled_job_id, run_date=None: scheduled.append(
            (scheduled_job_id, run_date)
        )
        or True,
    )
    previous_database = app_module.app.config["DATABASE"]
    try:
        app_module.app.config["DATABASE"] = str(db_path)
        summary = app_module.schedule_pending_background_jobs(app_module.app)
    finally:
        app_module.app.config["DATABASE"] = previous_database

    expected_utc = resume_lisbon.astimezone(UTC)
    assert scheduled == [(job_id, expected_utc)]
    assert summary["waiting_scheduled"] == 1

    conn = get_db(str(db_path))
    monkeypatch.setattr(
        app_module,
        "background_job_utc_now",
        lambda: expected_utc,
    )
    assert app_module.reactivate_due_rate_limited_background_jobs(conn) == 1
    status = conn.execute(
        "SELECT status FROM background_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()["status"]
    assert status == "pending"
    conn.close()


def test_legacy_naive_background_timestamp_is_interpreted_as_utc(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "legacy-naive.db"
    ensure_database(str(db_path))
    now_utc = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    conn = get_db(str(db_path))
    cursor = conn.execute(
        """
        INSERT INTO background_jobs (
            job_type, status, params_json, created_at, next_attempt_at
        ) VALUES (
            'fusionsolar_production_sync', 'waiting_api_slot', '{}',
            '2026-07-23T10:00:00', '2026-07-23T12:00:00'
        )
        """
    )
    job_id = int(cursor.lastrowid)
    conn.commit()
    monkeypatch.setattr(
        app_module,
        "background_job_utc_now",
        lambda: now_utc,
    )

    assert app_module.reactivate_due_rate_limited_background_jobs(conn) == 1
    status = conn.execute(
        "SELECT status FROM background_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()["status"]
    assert status == "pending"
    conn.close()


def test_background_job_timestamps_are_rendered_in_lisbon_time(tmp_path) -> None:
    db_path = tmp_path / "ui-time.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    conn.execute(
        """
        INSERT INTO background_jobs (
            job_type, status, params_json, created_at, next_attempt_at
        ) VALUES (
            'fusionsolar_production_sync', 'waiting_api_slot', '{}',
            '2026-07-23T01:00:00+00:00', '2026-07-23T02:00:00+00:00'
        )
        """
    )
    conn.commit()

    job = app_module.fetch_latest_background_jobs(conn, limit=1)[0]

    assert job["created_at"] == "2026-07-23T02:00:00+01:00"
    assert job["next_attempt_at"] == "2026-07-23T03:00:00+01:00"
    conn.close()
