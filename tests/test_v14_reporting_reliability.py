from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.services.production_api_queue import (
    WAT_HISTORY_AREA,
    release_api_lease,
    reserve_api_slot,
)
from monitoring_board.services.sampled_availability import (
    cleanup_realtime_snapshot_payloads,
    materialize_sampled_availability_day,
    record_device_configuration,
)


LISBON = ZoneInfo("Europe/Lisbon")


def _seed_asset_and_devices(
    conn: sqlite3.Connection,
    *,
    device_count: int = 2,
) -> tuple[int, list[int]]:
    asset_id = int(
        conn.execute(
            "INSERT INTO assets (project_name) VALUES ('Plant A')"
        ).lastrowid
    )
    device_ids: list[int] = []
    for index in range(device_count):
        device_id = int(
            conn.execute(
                """
                INSERT INTO provider_devices (
                    asset_id, provider, station_code, external_device_id,
                    device_name, dev_type_id, rated_power_kw, enabled,
                    created_at, updated_at
                ) VALUES (?, 'FusionSolar', 'S1', ?, ?, 1, 100, 1, ?, ?)
                """,
                (
                    asset_id,
                    f"D{index + 1}",
                    f"INV-{index + 1}",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            ).lastrowid
        )
        record_device_configuration(
            conn,
            provider_device_id=device_id,
            active=True,
            effective_date=date(2026, 1, 1),
        )
        device_ids.append(device_id)
    conn.commit()
    return asset_id, device_ids


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    device_id: int,
    when_lisbon: datetime,
    active_power_kw: float,
    availability_status: str = "available",
) -> None:
    collected_at = when_lisbon.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )
    conn.execute(
        """
        INSERT INTO device_realtime_snapshots (
            provider_device_id, asset_id, provider, station_code,
            collected_at, active_power_kw, availability_status,
            communication_status, pv_current_json, pv_voltage_json,
            payload_json, created_at
        ) VALUES (?, ?, 'FusionSolar', 'S1', ?, ?, ?, 'recent',
                  '{"pv1": 1}', '{"pv1": 500}', '{"raw": true}', ?)
        """,
        (
            device_id,
            asset_id,
            collected_at,
            active_power_kw,
            availability_status,
            collected_at,
        ),
    )


def test_sampled_availability_requires_per_inverter_window_coverage(
    tmp_path,
) -> None:
    db_path = tmp_path / "sampled.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    asset_id, device_ids = _seed_asset_and_devices(conn)
    target = date(2026, 7, 20)
    sample_times = (8, 9.5, 11, 12.5, 14)
    for device_id in device_ids:
        for hour_value in sample_times:
            hour = math.floor(hour_value)
            minute = int((hour_value - hour) * 60)
            _insert_snapshot(
                conn,
                asset_id=asset_id,
                device_id=device_id,
                when_lisbon=datetime(
                    2026,
                    7,
                    20,
                    hour,
                    minute,
                    tzinfo=LISBON,
                ),
                active_power_kw=10,
            )
    conn.commit()

    complete = materialize_sampled_availability_day(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        target_date=target,
    )
    assert complete["coverage_status"] == "sampled_complete"
    assert complete["availability_pct"] == 100
    assert complete["expected_inverters"] == 2
    assert complete["observed_inverters"] == 2

    conn.execute(
        """
        DELETE FROM device_realtime_snapshots
        WHERE provider_device_id = ?
        """,
        (device_ids[1],),
    )
    conn.commit()
    partial = materialize_sampled_availability_day(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        target_date=target,
    )
    assert partial["coverage_status"] == "sampled_partial"
    assert partial["availability_pct"] is None
    assert partial["observed_inverters"] == 1
    conn.close()


def test_no_observed_operating_window_is_not_numeric(tmp_path) -> None:
    db_path = tmp_path / "no-window.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    asset_id, device_ids = _seed_asset_and_devices(conn, device_count=1)
    _insert_snapshot(
        conn,
        asset_id=asset_id,
        device_id=device_ids[0],
        when_lisbon=datetime(2026, 7, 20, 12, tzinfo=LISBON),
        active_power_kw=0,
    )
    conn.commit()

    result = materialize_sampled_availability_day(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        target_date=date(2026, 7, 20),
    )
    assert result["coverage_status"] == "indeterminate"
    assert result["warning_code"] == "no_observed_operating_window"
    assert result["availability_pct"] is None
    conn.close()


def test_retention_materializes_then_clears_only_heavy_payloads(
    tmp_path,
) -> None:
    db_path = tmp_path / "retention.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    asset_id, device_ids = _seed_asset_and_devices(conn, device_count=1)
    _insert_snapshot(
        conn,
        asset_id=asset_id,
        device_id=device_ids[0],
        when_lisbon=datetime(2026, 5, 1, 12, tzinfo=LISBON),
        active_power_kw=0,
    )
    conn.commit()

    summary = cleanup_realtime_snapshot_payloads(
        conn,
        provider="FusionSolar",
        retention_days=30,
        reference_date=date(2026, 7, 24),
    )
    row = conn.execute(
        """
        SELECT active_power_kw, availability_status, payload_json,
               pv_current_json, pv_voltage_json
        FROM device_realtime_snapshots
        """
    ).fetchone()
    assert summary["snapshots_deleted"] == 0
    assert summary["payloads_cleared"] == 1
    assert row["active_power_kw"] == 0
    assert row["availability_status"] == "available"
    assert row["payload_json"] is None
    assert row["pv_current_json"] is None
    assert row["pv_voltage_json"] is None
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM plant_availability_sampled_daily"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_report_data_requests_are_local_deduplicated_and_skip_current_month(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "reports.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    asset_id = int(
        conn.execute(
            "INSERT INTO assets (project_name) VALUES ('Plant A')"
        ).lastrowid
    )
    conn.commit()
    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_kpi_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("report request must not call the API")
        ),
    )

    closed_period = app_module.build_period(
        "monthly",
        report_month="2026-06",
    )
    first = app_module.ensure_report_data_requests(
        conn,
        asset_ids=[asset_id],
        period=closed_period,
        include_wat=False,
        request_source="preview",
        reference_date=date(2026, 7, 24),
    )
    second = app_module.ensure_report_data_requests(
        conn,
        asset_ids=[asset_id],
        period=closed_period,
        include_wat=False,
        request_source="pdf",
        reference_date=date(2026, 7, 24),
    )
    current = app_module.ensure_report_data_requests(
        conn,
        asset_ids=[asset_id],
        period=app_module.build_period(
            "monthly",
            report_month="2026-07",
        ),
        include_wat=True,
        request_source="preview",
        reference_date=date(2026, 7, 24),
    )

    assert first["queued_count"] == 1
    assert second["job_ids"] == first["job_ids"]
    assert second["reused_count"] == 1
    assert current["job_ids"] == []
    assert (
        conn.execute(
            """
            SELECT COUNT(*) FROM background_jobs
            WHERE job_type = 'fusionsolar_report_production_request'
            """
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_wat_budget_default_stops_after_36_calls_until_midnight(
    tmp_path,
) -> None:
    conn = get_db(str(tmp_path / "wat-budget.db"))
    policy = app_module.fusionsolar_wat_policy()
    assert policy.daily_budget == 36
    start = datetime(2026, 7, 24, 0, 0, tzinfo=LISBON)
    for index in range(36):
        owner = f"wat-{index}"
        result = reserve_api_slot(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=WAT_HISTORY_AREA,
            lease_owner=owner,
            priority=5,
            policy=policy,
            now=start + timedelta(seconds=index),
        )
        assert result.granted
        release_api_lease(
            conn,
            provider="fusionsolar",
            account_key_value="account",
            api_area=WAT_HISTORY_AREA,
            lease_owner=owner,
            now=start + timedelta(seconds=index),
        )
    blocked = reserve_api_slot(
        conn,
        provider="fusionsolar",
        account_key_value="account",
        api_area=WAT_HISTORY_AREA,
        lease_owner="wat-36",
        priority=5,
        policy=policy,
        now=start + timedelta(seconds=36),
    )
    assert not blocked.granted
    assert blocked.wait_reason == "daily_budget"
    assert blocked.next_attempt_at == datetime(
        2026,
        7,
        25,
        0,
        0,
        tzinfo=LISBON,
    )
    conn.close()


def test_hourly_state_sync_reuses_daily_local_station_inventory(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "state-inventory.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    asset_id = int(
        conn.execute(
            "INSERT INTO assets (project_name) VALUES ('Plant A')"
        ).lastrowid
    )
    now = "2026-07-24T00:00:00+00:00"
    conn.execute(
        """
        INSERT OR REPLACE INTO integration_configs (
            provider, enabled, username, password, base_url,
            plants_endpoint, real_time_endpoint, created_at, updated_at
        ) VALUES (
            'FusionSolar', 1, 'user', 'secret', 'https://fusion.test',
            '/stations', '/realtime', ?, ?
        )
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO asset_integrations (
            asset_id, provider, external_id, external_name, enabled
        ) VALUES (?, 'FusionSolar', 'S1', 'Plant A', 1)
        """,
        (asset_id,),
    )
    app_module.set_app_state_value(
        conn,
        app_module.FUSIONSOLAR_STATION_INVENTORY_DATE_KEY,
        app_module.current_lisbon_date().isoformat(),
    )
    conn.commit()
    station_codes: list[str] = []
    monkeypatch.setattr(
        app_module,
        "get_fusionsolar_session",
        lambda *_args, **_kwargs: (object(), "token"),
    )
    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_stations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hourly sync must reuse local station inventory")
        ),
    )

    def capture_realtime(*_args, **kwargs):
        station_codes.extend(kwargs["station_codes"])
        return {"S1": {"stationCode": "S1", "dataItemMap": {}}}

    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_realtime_map",
        capture_realtime,
    )
    result = app_module.run_fusionsolar_check(
        conn,
        "FusionSolar",
        dry_run=True,
        include_diagnostics=False,
        prefer_local_station_inventory=True,
    )
    assert station_codes == ["S1"]
    assert result["station_inventory_source"] == "local"
    assert result["api_calls_used"] == 1
    conn.close()


def test_transient_sqlite_lock_becomes_auditable_wait(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "locked.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    job_id, _ = app_module.create_background_job(
        conn,
        "performance_reference_recalculation",
        {
            "period_date": "2026-06-01",
            "period_type": "month",
        },
    )
    conn.commit()
    conn.close()
    scheduled: list[int] = []
    monkeypatch.setattr(
        app_module,
        "run_background_job_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    monkeypatch.setattr(
        app_module,
        "schedule_background_job",
        lambda _app, queued_id, run_date=None: scheduled.append(queued_id)
        or True,
    )
    original_database = app_module.app.config["DATABASE"]
    app_module.app.config["DATABASE"] = str(db_path)
    try:
        app_module.run_background_job(app_module.app, job_id)
    finally:
        app_module.app.config["DATABASE"] = original_database

    conn = get_db(str(db_path))
    job = conn.execute(
        """
        SELECT status, wait_reason, next_attempt_at, result_json, params_json
        FROM background_jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    assert job["status"] == "waiting_api_slot"
    assert job["wait_reason"] == "database_locked"
    assert job["next_attempt_at"]
    assert json.loads(job["result_json"])["retry_attempt"] == 1
    assert json.loads(job["params_json"])["_sqlite_lock_attempt"] == 1
    assert scheduled == [job_id]
    conn.close()
