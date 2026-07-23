from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from zoneinfo import ZoneInfo

import app as app_module
import pytest
from app import ensure_database
from monitoring_board.db import get_db
from monitoring_board.services.fusionsolar_errors import FusionSolarRateLimitError
from monitoring_board.services.production_api_queue import (
    ApiQueuePolicy,
    PRODUCTION_KPI_AREA,
    ensure_api_queue_schema,
    list_api_queue_states,
    record_api_407,
    recover_expired_leases,
    release_api_lease,
    reserve_api_slot,
)


LISBON = ZoneInfo("Europe/Lisbon")
POLICY = ApiQueuePolicy(
    min_interval_seconds=65,
    daily_budget=20,
    reserved_calls_by_priority=((1, 2), (2, 2)),
    lease_seconds=300,
)


def test_fusionsolar_daily_and_monthly_kpis_share_one_account_queue() -> None:
    config = {
        "username": "user",
        "base_url": "https://fusion.test",
    }

    assert app_module.fusionsolar_production_account_key(
        config,
        "/day",
    ) == app_module.fusionsolar_production_account_key(
        config,
        "/month",
    )


def test_two_chunks_for_134_installations_are_separated_by_at_least_65_seconds(
    tmp_path,
) -> None:
    db_path = tmp_path / "queue.db"
    conn = get_db(str(db_path))
    ensure_api_queue_schema(conn)
    first_time = datetime(2026, 7, 23, 2, 0, tzinfo=LISBON)

    first = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="daily-chunk-1",
        priority=1,
        policy=POLICY,
        now=first_time,
    )
    release_api_lease(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="daily-chunk-1",
        now=first_time + timedelta(seconds=1),
    )
    second_early = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="daily-chunk-2",
        priority=1,
        policy=POLICY,
        now=first_time + timedelta(seconds=1),
    )
    second = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="daily-chunk-2",
        priority=1,
        policy=POLICY,
        now=first_time + timedelta(seconds=65),
    )

    assert first.granted is True
    assert second_early.granted is False
    assert second_early.wait_reason == "min_interval"
    assert second_early.next_attempt_at >= first_time + timedelta(seconds=65)
    assert second.granted is True
    conn.close()


def test_concurrent_jobs_never_receive_two_production_slots(
    tmp_path,
) -> None:
    db_path = tmp_path / "queue.db"
    conn = get_db(str(db_path))
    ensure_api_queue_schema(conn)
    conn.commit()
    conn.close()
    barrier = Barrier(2)
    now = datetime(2026, 7, 23, 2, 0, tzinfo=LISBON)

    def reserve(owner: str):
        thread_conn = get_db(str(db_path))
        barrier.wait()
        try:
            return reserve_api_slot(
                thread_conn,
                provider="fusionsolar",
                account_key_value="account",
                api_area=PRODUCTION_KPI_AREA,
                lease_owner=owner,
                priority=1 if owner == "daily" else 2,
                policy=POLICY,
                now=now,
            )
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ("daily", "month-close")))

    assert sum(result.granted for result in results) == 1
    assert {result.wait_reason for result in results if not result.granted} == {
        "active_lease"
    }


def test_daily_budget_reserves_four_calls_for_critical_work(
    tmp_path,
) -> None:
    conn = get_db(str(tmp_path / "queue.db"))
    ensure_api_queue_schema(conn)
    start = datetime(2026, 7, 23, 0, 0, tzinfo=LISBON)

    for index in range(16):
        now = start + timedelta(seconds=65 * index)
        result = reserve_api_slot(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=PRODUCTION_KPI_AREA,
            lease_owner=f"backfill-{index}",
            priority=3,
            policy=POLICY,
            now=now,
        )
        assert result.granted is True
        release_api_lease(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=PRODUCTION_KPI_AREA,
            lease_owner=f"backfill-{index}",
            now=now + timedelta(seconds=1),
        )

    blocked = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="backfill-blocked",
        priority=3,
        policy=POLICY,
        now=start + timedelta(seconds=65 * 16),
    )
    assert blocked.granted is False
    assert blocked.wait_reason == "reserved_budget"

    for index in range(2):
        now = start + timedelta(seconds=65 * (16 + index))
        critical = reserve_api_slot(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=PRODUCTION_KPI_AREA,
            lease_owner=f"critical-{index}",
            priority=1,
            policy=POLICY,
            now=now,
        )
        assert critical.granted is True
        release_api_lease(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=PRODUCTION_KPI_AREA,
            lease_owner=f"critical-{index}",
            now=now + timedelta(seconds=1),
        )

    daily_cannot_consume_month_close_reserve = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="daily-third-call",
        priority=1,
        policy=POLICY,
        now=start + timedelta(seconds=65 * 18),
    )
    assert daily_cannot_consume_month_close_reserve.granted is False
    assert daily_cannot_consume_month_close_reserve.wait_reason == "reserved_budget"

    for index in range(2):
        now = start + timedelta(seconds=65 * (18 + index))
        month_close = reserve_api_slot(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=PRODUCTION_KPI_AREA,
            lease_owner=f"month-close-{index}",
            priority=2,
            policy=POLICY,
            now=now,
        )
        assert month_close.granted is True
        release_api_lease(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=PRODUCTION_KPI_AREA,
            lease_owner=f"month-close-{index}",
            now=now + timedelta(seconds=1),
        )

    exhausted = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="daily-exhausted",
        priority=1,
        policy=POLICY,
        now=start + timedelta(seconds=65 * 20),
    )
    assert exhausted.granted is False
    assert exhausted.wait_reason == "daily_budget"
    assert exhausted.next_attempt_at == datetime(
        2026,
        7,
        24,
        0,
        0,
        tzinfo=LISBON,
    )
    conn.close()


def test_407_cooldown_is_scoped_to_provider_account_and_production_area(
    tmp_path,
) -> None:
    conn = get_db(str(tmp_path / "queue.db"))
    ensure_api_queue_schema(conn)
    now = datetime(2026, 7, 23, 3, 0, tzinfo=LISBON)
    cooldown_until = now + timedelta(hours=1)
    record_api_407(
        conn,
        provider="fusionsolar",
        account_key_value="fusion-account",
        api_area=PRODUCTION_KPI_AREA,
        cooldown_until=cooldown_until,
        now=now,
    )

    fusion = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="fusion-account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="fusion",
        priority=1,
        policy=POLICY,
        now=now + timedelta(minutes=1),
    )
    sigenergy = reserve_api_slot(
        conn,
        provider="sigenergy",
        account_key_value="sig-account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="sig",
        priority=1,
        policy=ApiQueuePolicy(None, None),
        now=now + timedelta(minutes=1),
    )

    assert fusion.granted is False
    assert fusion.wait_reason == "cooldown_407"
    assert sigenergy.granted is True
    conn.close()


def test_restart_preserves_slot_and_recovers_expired_lease(
    tmp_path,
) -> None:
    db_path = tmp_path / "queue.db"
    now = datetime(2026, 7, 23, 4, 0, tzinfo=LISBON)
    conn = get_db(str(db_path))
    ensure_api_queue_schema(conn)
    reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=PRODUCTION_KPI_AREA,
        lease_owner="crashed-worker",
        priority=1,
        policy=POLICY,
        now=now,
    )
    conn.close()

    restarted = get_db(str(db_path))
    before = list_api_queue_states(restarted, now=now + timedelta(minutes=1))[0]
    assert before["lease_owner"] == "crashed-worker"
    assert recover_expired_leases(
        restarted,
        now=now + timedelta(minutes=6),
    ) == 1
    after = list_api_queue_states(restarted, now=now + timedelta(minutes=6))[0]
    assert after["lease_owner"] is None
    assert after["daily_call_count"] == 1
    restarted.close()


def test_407_postpones_only_fusionsolar_production_jobs(
    tmp_path,
) -> None:
    db_path = tmp_path / "queue-jobs.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    conn.execute(
        """
        INSERT OR REPLACE INTO integration_configs (
            provider, enabled, username, password, base_url,
            day_kpi_endpoint, month_kpi_endpoint, created_at, updated_at
        ) VALUES (
            'FusionSolar', 1, 'user', 'secret', 'https://fusion.test',
            '/day', '/month', '2026-01-01', '2026-01-01'
        )
        """
    )
    current_job_id, _ = app_module.create_background_job(
        conn,
        "fusionsolar_production_sync",
        {
            "provider": "FusionSolar",
            "target_date": "2026-07-22",
            "period_type": "day",
        },
    )
    month_job_id, _ = app_module.create_background_job(
        conn,
        "fusionsolar_month_close",
        {
            "provider": "FusionSolar",
            "report_month": "2026-06",
        },
    )
    state_job_id, _ = app_module.create_background_job(
        conn,
        "fusionsolar_state_sync",
        {"provider": "FusionSolar"},
    )
    conn.commit()
    original_database = app_module.app.config["DATABASE"]
    app_module.app.config["DATABASE"] = str(db_path)
    try:
        with app_module.app.app_context():
            with app_module.production_kpi_call_context(
                job_id=current_job_id,
                job_type="fusionsolar_production_sync",
            ):
                with pytest.raises(FusionSolarRateLimitError):
                    app_module.execute_queued_fusionsolar_kpi_call(
                        lambda: (_ for _ in ()).throw(
                            FusionSolarRateLimitError(
                                "FusionSolar limitado",
                                payload={"failCode": 407},
                            )
                        ),
                        endpoint="/day",
                    )
    finally:
        app_module.app.config["DATABASE"] = original_database

    month_job = conn.execute(
        "SELECT status, wait_reason, next_attempt_at FROM background_jobs WHERE id = ?",
        (month_job_id,),
    ).fetchone()
    state_job = conn.execute(
        "SELECT status FROM background_jobs WHERE id = ?",
        (state_job_id,),
    ).fetchone()
    queue_state = list_api_queue_states(conn)[0]

    assert month_job["status"] == "waiting_api_slot"
    assert month_job["wait_reason"] == "cooldown_407"
    assert month_job["next_attempt_at"]
    assert state_job["status"] == "pending"
    assert queue_state["last_407_at"]
    assert queue_state["cooldown_until"]
    conn.close()


def test_waiting_api_slot_jobs_reactivate_after_restart_when_due(
    tmp_path,
) -> None:
    db_path = tmp_path / "waiting-jobs.db"
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    due_id = conn.execute(
        """
        INSERT INTO background_jobs (
            job_type, status, params_json, created_at, next_attempt_at, wait_reason
        ) VALUES (
            'fusionsolar_production_sync', 'waiting_api_slot', '{}', ?, ?, 'min_interval'
        )
        """,
        (
            now.isoformat(timespec="seconds"),
            (now - timedelta(seconds=1)).isoformat(timespec="seconds"),
        ),
    ).lastrowid
    future_id = conn.execute(
        """
        INSERT INTO background_jobs (
            job_type, status, params_json, created_at, next_attempt_at, wait_reason
        ) VALUES (
            'fusionsolar_month_close', 'waiting_api_slot', '{}', ?, ?, 'daily_budget'
        )
        """,
        (
            now.isoformat(timespec="seconds"),
            (now + timedelta(hours=1)).isoformat(timespec="seconds"),
        ),
    ).lastrowid
    conn.commit()

    assert app_module.reactivate_due_rate_limited_background_jobs(conn) == 1
    due = conn.execute(
        "SELECT status FROM background_jobs WHERE id = ?",
        (due_id,),
    ).fetchone()
    future = conn.execute(
        "SELECT status, wait_reason FROM background_jobs WHERE id = ?",
        (future_id,),
    ).fetchone()

    assert due["status"] == "pending"
    assert future["status"] == "waiting_api_slot"
    assert future["wait_reason"] == "daily_budget"
    conn.close()
