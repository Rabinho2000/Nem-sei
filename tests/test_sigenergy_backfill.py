from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.services.energy_facts import (
    parse_sigenergy_daily_history,
    persist_sigenergy_daily_history,
)
from monitoring_board.services.sigenergy_contracts import BackfillDayStatus
from monitoring_board.services.sigenergy_history import SigenergyBackfillService


HISTORY_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "sigenergy"
    / "system_history_day.json"
)


def _mapped_database(tmp_path, name: str):
    db_path = tmp_path / name
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    asset_id = int(
        conn.execute(
            "INSERT INTO assets (project_name) VALUES ('Backfill asset')"
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO asset_integrations (
            asset_id, provider, external_id, external_name, enabled
        ) VALUES (?, 'Sigenergy', 'SIG-BACKFILL', 'Backfill asset', 1)
        """,
        (asset_id,),
    )
    conn.commit()
    return conn, asset_id


def test_backfill_plans_one_job_for_each_of_thirty_completed_days(
    tmp_path,
) -> None:
    conn, asset_id = _mapped_database(tmp_path, "thirty-days.db")
    queued: list[tuple[str, date]] = []

    def enqueue(external_id: str, target_date: date) -> tuple[int, bool]:
        queued.append((external_id, target_date))
        return len(queued), True

    try:
        result = SigenergyBackfillService(
            conn,
            enqueue_day=enqueue,
            today=lambda: date(2026, 7, 30),
        ).plan(
            "SIG-BACKFILL",
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 30),
        )
    finally:
        conn.close()

    assert result.asset_id == asset_id
    assert result.queued_count == 30
    assert result.reused_count == 0
    assert result.complete_count == 0
    assert len(queued) == 30
    assert queued[0] == ("SIG-BACKFILL", date(2026, 6, 1))
    assert queued[-1] == ("SIG-BACKFILL", date(2026, 6, 30))


def test_backfill_resume_skips_only_fully_materialized_days(tmp_path) -> None:
    conn, asset_id = _mapped_database(tmp_path, "resume.db")
    payload = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))["data"]
    fact = parse_sigenergy_daily_history(
        payload,
        system_id="SIG-BACKFILL",
        period_date=date(2026, 6, 1),
        confirmed_unit="kWh",
    )
    persist_sigenergy_daily_history(conn, asset_id=asset_id, fact=fact)
    queued: list[date] = []

    def enqueue(_external_id: str, target_date: date) -> tuple[int, bool]:
        queued.append(target_date)
        return len(queued), True

    try:
        result = SigenergyBackfillService(
            conn,
            enqueue_day=enqueue,
            today=lambda: date(2026, 7, 30),
        ).plan(
            "SIG-BACKFILL",
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 3),
        )
    finally:
        conn.close()

    assert [day.status for day in result.days] == [
        BackfillDayStatus.COMPLETE,
        BackfillDayStatus.QUEUED,
        BackfillDayStatus.QUEUED,
    ]
    assert queued == [date(2026, 6, 2), date(2026, 6, 3)]


def test_background_backfill_jobs_are_deduplicated_by_system_and_day(
    tmp_path,
    monkeypatch,
) -> None:
    conn, _asset_id = _mapped_database(tmp_path, "deduplicated.db")
    monkeypatch.setattr(
        app_module,
        "current_lisbon_date",
        lambda: date(2026, 7, 30),
    )
    try:
        first = app_module.enqueue_sigenergy_energy_backfill(
            conn,
            external_id="SIG-BACKFILL",
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 2),
        )
        second = app_module.enqueue_sigenergy_energy_backfill(
            conn,
            external_id="SIG-BACKFILL",
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 2),
        )
        jobs = conn.execute(
            """
            SELECT id, params_json
            FROM background_jobs
            WHERE job_type = 'sigenergy_energy_sync'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert first == [(1, True), (2, True)]
    assert second == [(1, False), (2, False)]
    assert len(jobs) == 2
    assert [
        json.loads(row["params_json"])["target_date"] for row in jobs
    ] == ["2026-06-01", "2026-06-02"]


def test_backfill_rejects_unmapped_system_and_unfinished_day(tmp_path) -> None:
    db_path = tmp_path / "invalid.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        service = SigenergyBackfillService(
            conn,
            enqueue_day=lambda _external_id, _target_date: (1, True),
            today=lambda: date(2026, 7, 30),
        )
        try:
            service.plan(
                "SIG-UNMAPPED",
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 1),
            )
        except ValueError as exc:
            assert "associada e ativa" in str(exc)
        else:
            raise AssertionError("unmapped backfill was accepted")

    conn, _asset_id = _mapped_database(tmp_path, "unfinished.db")
    try:
        service = SigenergyBackfillService(
            conn,
            enqueue_day=lambda _external_id, _target_date: (1, True),
            today=lambda: date(2026, 7, 30),
        )
        try:
            service.plan(
                "SIG-BACKFILL",
                date_from=date(2026, 7, 29),
                date_to=date(2026, 7, 30),
            )
        except ValueError as exc:
            assert "dias ja terminados" in str(exc)
        else:
            raise AssertionError("unfinished backfill day was accepted")
    finally:
        conn.close()
