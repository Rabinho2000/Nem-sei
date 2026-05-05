from __future__ import annotations

from datetime import date
from typing import Any

import pytest

import app as app_module
from app import ensure_database, run_fusionsolar_production_backfill
from monitoring_board.db import get_db


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
    monkeypatch.setattr(app_module, "get_fusionsolar_session", lambda _config: (object(), "token"))


def kpi_row(station_code: str, collect_date: date, value: float = 100) -> dict[str, Any]:
    return {
        "stationCode": station_code,
        "collectTime": app_module.collect_time_ms(collect_date),
        "dataItemMap": {"PVYield": str(value)},
        "payload_json": "{}",
    }


def kpi_map(station_code: str, collect_date: date, value: float = 100) -> dict[str, Any]:
    return {station_code: kpi_row(station_code, collect_date, value)}


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


def test_rate_limit_stops_backfill_without_repeating_requests(conn, monkeypatch: pytest.MonkeyPatch) -> None:
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
    )

    assert calls["total"] == 1
    assert result["api_errors"] == 1
    assert "temporariamente limitada" in result["stopped_reason"]
    assert result["baselines_recalculated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM production_records WHERE period_type = 'day'").fetchone()[0] == 0


def test_rate_limit_cooldown_skips_next_backfill_without_api_call(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    add_asset(conn, "Central A", "S1")
    fake_session(monkeypatch)
    calls = {"total": 0}

    def fetch_day_rows(_session, _base_url, _endpoint, station_codes, collect_date):
        calls["total"] += 1
        raise ValueError("Falha ao obter os KPIs diarios FusionSolar. (failCode=407)")

    monkeypatch.setattr(app_module, "fetch_fusionsolar_kpi_day_rows", fetch_day_rows)
    install_month_mtd(monkeypatch)

    first = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )
    second = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert calls["total"] == 1
    assert "temporariamente limitada" in first["stopped_reason"]
    assert "temporariamente limitada" in second["stopped_reason"]
    assert second["records_updated"] == 0


def test_rate_limit_cooldown_is_persisted_in_database(conn, monkeypatch: pytest.MonkeyPatch) -> None:
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
    )
    app_module.FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL = None
    second = run_fusionsolar_production_backfill(
        conn,
        period_type="day",
        from_year=2026,
        to_year=2026,
        today_value=date(2026, 2, 1),
        kpi_call_delay_seconds=0,
    )

    assert calls["total"] == 1
    assert "temporariamente limitada" in second["stopped_reason"]


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
