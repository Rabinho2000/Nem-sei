from __future__ import annotations

from datetime import date
from typing import Any

import pytest

import app as app_module
from app import (
    ensure_database,
    run_fusionsolar_month_close,
    run_fusionsolar_month_cycle,
    run_fusionsolar_production_backfill,
)
from monitoring_board.db import get_db
from monitoring_board.reporting.financial_quality import apply_production_financial_gate


@pytest.fixture()
def conn(tmp_path):
    app_module.FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL = None
    db_path = tmp_path / "backfill.db"
    ensure_database(str(db_path))
    connection = get_db(str(db_path))
    connection.execute(
        """
        INSERT INTO integration_configs (
            provider, username, password, base_url, login_endpoint, plants_endpoint,
            real_time_endpoint, alarms_endpoint, day_kpi_endpoint, month_kpi_endpoint,
            enabled, auto_sync_enabled, sync_hours, created_at, updated_at
        ) VALUES ('FusionSolar', 'user', 'secret', 'https://fusion.test', '/login', '/stations',
            '/real', '/alarms', '/day', '/month', 1, 0, '08:00', '2026-01-01', '2026-01-01')
        """
    )
    try:
        yield connection
    finally:
        app_module.FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL = None
        connection.close()


def add_asset(conn, name: str, station_code: str, kwp: str = "50") -> int:
    cursor = conn.execute("INSERT INTO assets (project_name, kwp) VALUES (?, ?)", (name, kwp))
    asset_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
        VALUES (?, 'FusionSolar', ?, ?, 1)
        """,
        (asset_id, station_code, name),
    )
    conn.commit()
    return asset_id


def fake_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "get_fusionsolar_session", lambda _config, force_login=False: (object(), "token"))


def kpi_row(station_code: str, collect_date: date, value: float = 100) -> dict[str, Any]:
    return {
        "stationCode": station_code,
        "collectTime": app_module.collect_time_ms(collect_date),
        "dataItemMap": {"PVYield": str(value)},
        "payload_json": "{}",
    }


def kpi_map(station_code: str, collect_date: date, value: float = 100) -> dict[str, Any]:
    return {station_code: kpi_row(station_code, collect_date, value)}


def insert_production_record(
    conn,
    asset_id: int,
    period_type: str,
    period_date: date,
    production_kwh: float | None,
) -> None:
    conn.execute(
        """
        INSERT INTO production_records (
            asset_id, provider, external_id, period_type, period_date, production_kwh,
            performance_status, data_quality, payload_json, created_at, updated_at
        ) VALUES (?, 'FusionSolar', 'S1', ?, ?, ?, 'OK', 'ok', '{}', '2026-01-01', '2026-01-01')
        """,
        (asset_id, period_type, period_date.isoformat(), production_kwh),
    )
    conn.commit()


def install_month_rows(monkeypatch: pytest.MonkeyPatch, rows_by_month: dict[str, list[dict[str, Any]]] | None = None):
    calls: list[tuple[tuple[str, ...], date]] = []

    def fetch_day_rows(_session, _base_url, _endpoint, station_codes, collect_date):
        calls.append((tuple(station_codes), collect_date))
        return list(rows_by_month.get(collect_date.isoformat(), [])) if rows_by_month is not None else [
            kpi_row(station_codes[0], collect_date, 10)
        ]

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    return calls


def install_month_mtd(monkeypatch: pytest.MonkeyPatch, value: float = 999) -> None:
    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_kpi_month_map",
        lambda _session, _base_url, _endpoint, station_codes, collect_date: {
            code: kpi_row(code, collect_date, value) for code in station_codes
        },
    )


def test_daily_backfill_skips_current_and_future_days(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {
        "2026-01-01": [
            kpi_row("S1", date(2026, 1, 1), 10),
            kpi_row("S1", date(2026, 1, 2), 20),
            kpi_row("S1", date(2026, 1, 3), 30),
        ]
    }
    install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 1, 3),
        kpi_call_delay_seconds=0,
    )

    days = [
        row["period_date"]
        for row in conn.execute("SELECT period_date FROM production_records WHERE period_type = 'day' ORDER BY period_date")
    ]
    assert days == ["2026-01-01", "2026-01-02"]


def test_daily_backfill_calls_fusionsolar_once_per_month_not_once_per_day(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {
        "2026-01-01": [kpi_row("S1", date(2026, 1, 1), 10), kpi_row("S1", date(2026, 1, 2), 20)],
        "2026-02-01": [kpi_row("S1", date(2026, 2, 1), 30)],
    }
    calls = install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 3, 1),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 2, 28),
        kpi_call_delay_seconds=0,
    )

    assert [call[1] for call in calls] == [date(2026, 1, 1), date(2026, 2, 1)]
    assert conn.execute("SELECT COUNT(*) FROM production_records WHERE period_type = 'day'").fetchone()[0] == 3


def test_daily_backfill_chunks_station_codes_at_100(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(101):
        add_asset(conn, f"Central {index}", f"S{index}")
    fake_session(monkeypatch)
    calls = install_month_rows(monkeypatch, {})
    install_month_mtd(monkeypatch)

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert len(calls) == 2
    assert len(calls[0][0]) == 100
    assert len(calls[1][0]) == 1


def test_daily_backfill_respects_date_window(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {
        "2026-01-01": [kpi_row("S1", date(2026, 1, day), day) for day in range(1, 10)]
    }
    install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 1, 10),
        date_from=date(2026, 1, 4),
        date_to=date(2026, 1, 6),
        kpi_call_delay_seconds=0,
    )

    days = [
        row["period_date"]
        for row in conn.execute("SELECT period_date FROM production_records WHERE period_type = 'day' ORDER BY period_date")
    ]
    assert days == ["2026-01-04", "2026-01-05", "2026-01-06"]


def test_daily_backfill_respects_max_api_calls(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {
        "2026-01-01": [kpi_row("S1", date(2026, 1, 1), 10)],
        "2026-02-01": [kpi_row("S1", date(2026, 2, 1), 20)],
    }
    calls = install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)

    result = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 3, 1),
        max_api_calls=1,
        kpi_call_delay_seconds=0,
    )

    assert len(calls) == 1
    assert result["api_calls_used"] == 1
    assert "Limite local" in result["stopped_reason"]
    assert conn.execute("SELECT COUNT(*) FROM production_records WHERE period_type = 'day'").fetchone()[0] == 1


def test_monthly_backfill_creates_monthly_production_records(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    install_month_mtd(monkeypatch, 100)

    run_fusionsolar_production_backfill(
        conn,
        period_type="month",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 3, 15),
        kpi_call_delay_seconds=0,
    )

    months = [
        row["period_date"]
        for row in conn.execute("SELECT period_date FROM production_records WHERE period_type = 'month' ORDER BY period_date")
    ]
    assert months == ["2026-01-01", "2026-02-01"]


def test_backfill_upsert_avoids_duplicates(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {"2026-01-01": [kpi_row("S1", date(2026, 1, 1), 10), kpi_row("S1", date(2026, 1, 2), 20)]}
    install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)

    kwargs = {
        "period_type": "day",
        "from_year": 2026,
        "to_year": 2026,
        "today_value": date(2026, 1, 3),
        "kpi_call_delay_seconds": 0,
    }
    run_fusionsolar_production_backfill(conn, **kwargs)
    run_fusionsolar_production_backfill(conn, **kwargs)

    count = conn.execute("SELECT COUNT(*) FROM production_records WHERE period_type = 'day'").fetchone()[0]
    assert count == 2


def test_rate_limit_backfill_stops_after_repeated_wait_cycles(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    add_asset(conn, "Central B", "S2")
    fake_session(monkeypatch)
    calls = {"total": 0}

    def fetch_day_rows(_session, _base_url, _endpoint, station_codes, collect_date):
        calls["total"] += 1
        raise ValueError("Falha ao obter os KPIs diarios FusionSolar. (failCode=407)")

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    install_month_mtd(monkeypatch)

    result = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
        sleeper=lambda _seconds: None,
        max_wait_cycles=1,
    )

    assert calls["total"] == 2
    assert result["api_errors"] == 2
    assert result["wait_cycles"] == 2
    assert "repetido demasiadas vezes" in result["stopped_reason"]
    assert result["baselines_recalculated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM production_records WHERE period_type = 'day'").fetchone()[0] == 0


def test_rate_limit_backfill_waits_and_continues(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    calls = {"total": 0}
    sleeps: list[float] = []

    def fetch_day_rows(_session, _base_url, _endpoint, station_codes, collect_date):
        calls["total"] += 1
        if calls["total"] == 1:
            raise ValueError("Falha ao obter os KPIs diarios FusionSolar. (failCode=407)")
        return [kpi_row("S1", date(2026, 1, 1), 10)]

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    install_month_mtd(monkeypatch)

    result = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
        sleeper=sleeps.append,
    )

    assert calls["total"] == 2
    assert result["wait_cycles"] == 1
    assert result["stopped_reason"] == ""
    assert result["records_updated"] == 1
    assert sleeps and sleeps[0] > 0


def test_backfill_refreshes_session_after_relogin_error(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    sessions: list[str] = []
    calls = {"total": 0}

    def get_session(_config, force_login=False):
        token = "forced" if force_login else "cached"
        sessions.append(token)
        return token, "token"

    def fetch_day_rows(session_obj, _base_url, _endpoint, station_codes, collect_date):
        calls["total"] += 1
        if calls["total"] == 1:
            raise ValueError("USER_MUST_RELOGIN (failCode=305)")
        assert session_obj == "forced"
        return [kpi_row("S1", date(2026, 1, 1), 10)]

    monkeypatch.setattr(app_module, "get_fusionsolar_session", get_session)
    monkeypatch.setattr(app_module, "invalidate_fusionsolar_session", lambda _config: None)
    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    install_month_mtd(monkeypatch)

    result = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert sessions == ["cached", "forced"]
    assert calls["total"] == 2
    assert result["records_updated"] == 1
    assert result["stopped_reason"] == ""


def test_rate_limit_cooldown_is_persisted_and_waited(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    calls = {"total": 0}

    def fetch_day_rows(_session, _base_url, _endpoint, station_codes, collect_date):
        calls["total"] += 1
        raise ValueError("Falha ao obter os KPIs diarios FusionSolar. (failCode=407)")

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    install_month_mtd(monkeypatch)

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
        sleeper=lambda _seconds: None,
        max_wait_cycles=1,
    )
    app_module.FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL = None
    second = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
        sleeper=lambda _seconds: None,
        max_wait_cycles=1,
    )

    assert calls["total"] == 3
    assert second["wait_cycles"] == 2
    assert "repetido demasiadas vezes" in second["stopped_reason"]


def test_rate_limit_records_single_api_limit_alert(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)

    def fetch_day_rows(_session, _base_url, _endpoint, station_codes, collect_date):
        raise ValueError("Falha ao obter os KPIs diarios FusionSolar. (failCode=407)")

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    install_month_mtd(monkeypatch)

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
        sleeper=lambda _seconds: None,
        max_wait_cycles=1,
    )
    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
        sleeper=lambda _seconds: None,
        max_wait_cycles=1,
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_alerts WHERE alert_type = 'fusionsolar_api_limit'"
    ).fetchone()[0] == 1


def test_rate_limit_alert_is_throttled_for_repeated_marks(conn) -> None:
    app_module.mark_fusionsolar_performance_rate_limited(conn)
    app_module.mark_fusionsolar_performance_rate_limited(conn)
    app_module.mark_fusionsolar_performance_rate_limited(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_alerts WHERE alert_type = 'fusionsolar_api_limit'"
    ).fetchone()[0] == 1


def test_no_real_sleep_happens_when_sleeper_is_injected(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {
        "2026-01-01": [kpi_row("S1", date(2026, 1, 1), 10)],
        "2026-02-01": [kpi_row("S1", date(2026, 2, 1), 20)],
    }
    install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)
    sleeps: list[float] = []

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 3, 1),
        sleeper=sleeps.append,
        kpi_call_delay_seconds=65,
    )

    assert sleeps == [65, 65]


def test_month_cycle_imports_selected_assets_for_month(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)

    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_kpi_day_rows",
        lambda _session, _base_url, _endpoint, station_codes, collect_date: [
            kpi_row("S1", date(2026, 1, 1), 10),
            kpi_row("S1", date(2026, 1, 2), 20),
        ],
    )
    install_month_mtd(monkeypatch, 30)

    result = run_fusionsolar_month_cycle(
        conn,
        report_month="2026-01",
        asset_ids=[asset_id],
        kpi_call_delay_seconds=0,
    )

    assert result["status"] == "completed"
    assert result["records_updated"] == 2
    assert result["monthly_records_updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM production_records WHERE asset_id = ?", (asset_id,)).fetchone()[0] == 3


def test_month_cycle_waits_and_continues_after_rate_limit(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    calls = {"day": 0}
    sleeps: list[float] = []

    def fetch_day_rows(_session, _base_url, _endpoint, station_codes, collect_date):
        calls["day"] += 1
        if calls["day"] == 1:
            raise ValueError("Falha ao obter os KPIs diarios FusionSolar. (failCode=407)")
        return [kpi_row("S1", date(2026, 1, 1), 10)]

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    install_month_mtd(monkeypatch, 10)

    result = run_fusionsolar_month_cycle(
        conn,
        report_month="2026-01",
        asset_ids=[asset_id],
        kpi_call_delay_seconds=0,
        sleeper=sleeps.append,
    )

    assert calls["day"] == 2
    assert result["wait_cycles"] == 1
    assert result["status"] == "completed"
    assert sleeps and sleeps[0] > 0


def test_month_close_skips_api_when_monthly_value_is_valid_with_partial_daily_coverage(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = add_asset(conn, "Central A", "S1")
    insert_production_record(conn, asset_id, "month", date(2026, 1, 1), 100)
    insert_production_record(conn, asset_id, "day", date(2026, 1, 1), 10)
    monkeypatch.setattr(
        app_module,
        "get_fusionsolar_session",
        lambda *_args, **_kwargs: pytest.fail("A API nao deve ser chamada para um mes ja completo."),
    )

    result = run_fusionsolar_month_close(
        conn,
        report_month="2026-01",
        reference_date=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert result["states_before"]["complete"] == 1
    assert result["states_after"]["complete"] == 1
    assert result["api_calls_used"] == 0
    assert result["daily_assets_requested"] == 0


def test_month_close_treats_zero_monthly_production_as_complete(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = add_asset(conn, "Central Zero", "S1")
    insert_production_record(conn, asset_id, "month", date(2026, 1, 1), 0)
    monkeypatch.setattr(
        app_module,
        "get_fusionsolar_session",
        lambda *_args, **_kwargs: pytest.fail("Zero mensal valido nao deve acionar a API."),
    )

    result = run_fusionsolar_month_close(
        conn,
        report_month="2026-01",
        reference_date=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert result["states_after"] == {"complete": 1, "partial": 0, "missing": 0, "conflict": 0}
    assert result["api_calls_used"] == 0


def test_month_close_uses_complete_daily_data_when_monthly_production_is_missing(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    monthly_calls: list[tuple[str, ...]] = []
    daily_calls: list[tuple[str, ...]] = []

    def fetch_month(_session, _base_url, _endpoint, station_codes, _collect_date):
        monthly_calls.append(tuple(station_codes))
        return {}

    def fetch_days(_session, _base_url, _endpoint, station_codes, _collect_date):
        daily_calls.append(tuple(station_codes))
        return [kpi_row("S1", date(2026, 1, day), 1) for day in range(1, 32)]

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_month_map", fetch_month)
    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_days)

    result = run_fusionsolar_month_close(
        conn,
        report_month="2026-01",
        reference_date=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )
    quality = app_module.evaluate_local_monthly_production_quality(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        month_start=date(2026, 1, 1),
        reference_date=date(2026, 2, 1),
    )

    assert result["states_before"]["missing"] == 1
    assert result["states_after"]["complete"] == 1
    assert quality.status == "complete"
    assert quality.source == "daily"
    assert monthly_calls == [("S1",)]
    assert daily_calls == [("S1",)]


def test_month_close_chunks_monthly_requests_at_100_installations(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(101):
        add_asset(conn, f"Central {index}", f"S{index}")
    fake_session(monkeypatch)
    chunk_sizes: list[int] = []

    def fetch_month(_session, _base_url, _endpoint, station_codes, collect_date):
        chunk_sizes.append(len(station_codes))
        return {
            code: kpi_row(code, collect_date, 10)
            for code in station_codes
        }

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_month_map", fetch_month)
    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_kpi_day_rows",
        lambda *_args, **_kwargs: pytest.fail("O KPI mensal valido deve evitar fallback diario."),
    )
    monkeypatch.setattr(
        app_module,
        "recalculate_performance_references",
        lambda *_args, **_kwargs: {
            "records_processed": 1,
            "references_created": 0,
            "still_without_reference": 1,
            "missing_kwp": 0,
            "missing_production": 0,
        },
    )

    result = run_fusionsolar_month_close(
        conn,
        report_month="2026-01",
        reference_date=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert chunk_sizes == [100, 1]
    assert result["api_calls_used"] == 2
    assert result["states_after"]["complete"] == 101


def test_month_close_rechecks_conflict_after_monthly_refresh_without_daily_fallback(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = add_asset(conn, "Central A", "S1")
    insert_production_record(conn, asset_id, "month", date(2026, 1, 1), 100)
    for day in range(1, 32):
        insert_production_record(conn, asset_id, "day", date(2026, 1, day), 200 / 31)
    fake_session(monkeypatch)
    monthly_calls: list[tuple[str, ...]] = []

    def fetch_month(_session, _base_url, _endpoint, station_codes, collect_date):
        monthly_calls.append(tuple(station_codes))
        return kpi_map("S1", collect_date, 200)

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_month_map", fetch_month)
    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_kpi_day_rows",
        lambda *_args, **_kwargs: pytest.fail("O conflito resolvido pelo mensal nao deve pedir diario."),
    )

    result = run_fusionsolar_month_close(
        conn,
        report_month="2026-01",
        reference_date=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert result["states_before"]["conflict"] == 1
    assert result["states_after_monthly"]["complete"] == 1
    assert result["states_after"]["complete"] == 1
    assert result["daily_assets_requested"] == 0
    assert monthly_calls == [("S1",)]


@pytest.mark.parametrize("report_month", ["2026-02", "2026-03"])
def test_month_close_never_queries_current_or_future_month(
    conn,
    monkeypatch: pytest.MonkeyPatch,
    report_month: str,
) -> None:
    add_asset(conn, "Central A", "S1")
    monkeypatch.setattr(
        app_module,
        "get_fusionsolar_session",
        lambda *_args, **_kwargs: pytest.fail("Mes atual ou futuro nao pode consultar a API."),
    )

    with pytest.raises(ValueError, match="meses civis anteriores"):
        run_fusionsolar_month_close(
            conn,
            report_month=report_month,
            reference_date=date(2026, 2, 15),
            kpi_call_delay_seconds=0,
        )


def test_month_close_relogs_once_after_305(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_asset(conn, "Central A", "S1")
    login_calls: list[bool] = []
    month_calls = 0

    def get_session(_config, force_login=False):
        login_calls.append(force_login)
        return object(), "token"

    def fetch_month(_session, _base_url, _endpoint, station_codes, collect_date):
        nonlocal month_calls
        month_calls += 1
        if month_calls == 1:
            raise ValueError("USER_MUST_RELOGIN (failCode=305)")
        return {code: kpi_row(code, collect_date, 20) for code in station_codes}

    monkeypatch.setattr(app_module, "get_fusionsolar_session", get_session)
    monkeypatch.setattr(app_module, "invalidate_fusionsolar_session", lambda _config: None)
    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_month_map", fetch_month)
    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_kpi_day_rows",
        lambda *_args, **_kwargs: pytest.fail("O mensal valido apos relogin deve finalizar o mes."),
    )

    result = run_fusionsolar_month_close(
        conn,
        report_month="2026-01",
        reference_date=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert login_calls == [False, True]
    assert month_calls == 2
    assert result["api_calls_attempted"] == 2
    assert result["states_after"]["complete"] == 1


def test_month_close_407_waits_persistently_and_reuses_same_job(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "fetch_fusionsolar_kpi_month_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Falha ao obter os KPIs mensais FusionSolar. (failCode=407)")
        ),
    )
    monkeypatch.setattr(app_module, "current_lisbon_date", lambda: date(2026, 2, 3))
    monkeypatch.setattr(app_module, "schedule_background_job", lambda *_args, **_kwargs: True)
    params = {
        "provider": "FusionSolar",
        "report_month": "2026-01",
        "trigger_type": "scheduled_month_close",
    }
    job_id, created = app_module.create_background_job(conn, "fusionsolar_month_close", params)
    conn.commit()
    database_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    original_database = app_module.app.config["DATABASE"]
    app_module.app.config["DATABASE"] = database_path
    try:
        app_module.run_background_job(app_module.app, job_id)
    finally:
        app_module.app.config["DATABASE"] = original_database

    job = conn.execute(
        "SELECT status, result_json, next_attempt_at FROM background_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    duplicate_id, duplicate_created = app_module.create_background_job(
        conn,
        "fusionsolar_month_close",
        {**params, "trigger_type": "reactivation_probe"},
    )
    result = app_module.json.loads(job["result_json"])

    assert created is True
    assert job["status"] == "waiting_rate_limit"
    assert job["next_attempt_at"]
    assert result["month"] == "2026-01"
    assert result["states_before"]["missing"] == 1
    assert result["states_after"]["missing"] == 1
    assert result["api_calls_attempted"] == 1
    assert result["next_attempt_at"] == job["next_attempt_at"]
    assert duplicate_created is False
    assert duplicate_id == job_id


def test_month_close_does_not_change_snapshots_and_keeps_financials_draft_when_missing(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = add_asset(conn, "Central A", "S1")
    portfolio_id = conn.execute("INSERT INTO portfolio_groups (name) VALUES ('Snapshot antigo')").lastrowid
    snapshot_id = conn.execute(
        """
        INSERT INTO portfolio_report_runs (
            portfolio_id, report_month, created_at, notes, summary_json, warnings_json, rows_json
        ) VALUES (?, '2025-12', '2026-01-01', 'imutavel', '{"old": true}', '[]', '[{"old": true}]')
        """,
        (portfolio_id,),
    ).lastrowid
    conn.commit()
    snapshot_before = dict(
        conn.execute("SELECT * FROM portfolio_report_runs WHERE id = ?", (snapshot_id,)).fetchone()
    )
    fake_session(monkeypatch)
    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_month_map", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", lambda *_args, **_kwargs: [])

    result = run_fusionsolar_month_close(
        conn,
        report_month="2026-01",
        reference_date=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )
    snapshot_after = dict(
        conn.execute("SELECT * FROM portfolio_report_runs WHERE id = ?", (snapshot_id,)).fetchone()
    )
    quality = app_module.evaluate_local_monthly_production_quality(
        conn,
        asset_id=asset_id,
        provider="FusionSolar",
        month_start=date(2026, 1, 1),
        reference_date=date(2026, 2, 1),
    )
    financial_values = {
        "production_quality_status": quality.status,
        "estimated_value_eur": 123.45,
    }
    apply_production_financial_gate(financial_values)

    assert result["states_after"]["missing"] == 1
    assert snapshot_after == snapshot_before
    assert financial_values["estimated_value_eur"] is None


def test_reference_recalculation_only_runs_for_imported_dates(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {"2026-01-01": [kpi_row("S1", date(2026, 1, 2), 10)]}
    install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)
    recalculated: list[str] = []

    def fake_recalculate(_conn, **kwargs):
        if kwargs.get("period_type") == "day":
            recalculated.append(kwargs["period_date"].isoformat())
        return {"records_processed": 1, "references_created": 0, "still_without_reference": 1, "missing_kwp": 0, "missing_production": 0}

    monkeypatch.setattr(app_module, "recalculate_performance_references", fake_recalculate)

    run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        kpi_call_delay_seconds=0,
    )

    assert recalculated == ["2026-01-02"]


def test_mtd_recalculation_after_backfill(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    rows = {"2026-01-01": [kpi_row("S1", date(2026, 1, 1), 10), kpi_row("S1", date(2026, 1, 2), 10)]}
    install_month_rows(monkeypatch, rows)
    install_month_mtd(monkeypatch)

    result = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 1, 3),
        kpi_call_delay_seconds=0,
    )

    mtd = conn.execute(
        "SELECT production_kwh FROM production_records WHERE period_type = 'mtd' AND period_date = '2026-01-01'"
    ).fetchone()
    assert result["mtd_records_updated"] == 1
    assert mtd["production_kwh"] == 999
