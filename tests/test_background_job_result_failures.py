from __future__ import annotations

import json
from datetime import date

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.repositories.sigenergy import history_day_is_complete
from monitoring_board.services.energy_facts import (
    parse_sigenergy_daily_history,
    persist_sigenergy_daily_history,
)


def _run_job_against_database(tmp_path, monkeypatch, payload):
    db_path = tmp_path / "returned-failure.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        job_id, _ = app_module.create_background_job(
            conn,
            "sigenergy_energy_sync",
            {"external_id": "SIG-FAIL", "target_date": "2026-07-01"},
        )
        conn.commit()
    scheduled: list[tuple[int, object]] = []
    monkeypatch.setattr(app_module, "run_background_job_payload", lambda *_args: payload)
    monkeypatch.setattr(
        app_module,
        "schedule_background_job",
        lambda _app, queued_id, run_date=None: scheduled.append((queued_id, run_date)) or True,
    )
    previous_database = app_module.app.config["DATABASE"]
    app_module.app.config["DATABASE"] = str(db_path)
    try:
        app_module.run_background_job(app_module.app, job_id)
    finally:
        app_module.app.config["DATABASE"] = previous_database
    return db_path, job_id, scheduled


def test_returned_sigenergy_502_becomes_a_limited_retry(tmp_path, monkeypatch) -> None:
    payload = {
        "status": "failed",
        "data_quality": "invalid",
        "message": "Sigenergy HTTP 502",
        "error": {"category": "provider_error", "api_code": "502"},
    }
    db_path, job_id, scheduled = _run_job_against_database(tmp_path, monkeypatch, payload)

    with get_db(str(db_path)) as conn:
        job = conn.execute(
            """
            SELECT status, attempt_count, wait_reason, next_attempt_at,
                   error_category, external_error_code, result_json
            FROM background_jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()

    assert job["status"] == "waiting_api_slot"
    assert job["attempt_count"] == 1
    assert job["wait_reason"] == "recoverable_failure"
    assert job["next_attempt_at"]
    assert job["error_category"] == "provider_error"
    assert job["external_error_code"] == "502"
    assert json.loads(job["result_json"])["status"] == "retrying"
    assert scheduled and scheduled[0][0] == job_id


def test_returned_unconfirmed_unit_is_a_visible_permanent_failure(tmp_path, monkeypatch) -> None:
    payload = {
        "status": "failed",
        "data_quality": "invalid",
        "message": "A unidade do historico Sigenergy ainda nao foi confirmada como kWh.",
        "error": {"category": "provider_error"},
    }
    db_path, job_id, scheduled = _run_job_against_database(tmp_path, monkeypatch, payload)

    with get_db(str(db_path)) as conn:
        job = conn.execute(
            """
            SELECT status, attempt_count, next_attempt_at, error_message,
                   error_category FROM background_jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()

    assert job["status"] == "failed"
    assert job["attempt_count"] == 1
    assert job["next_attempt_at"] is None
    assert "unidade" in job["error_message"]
    assert job["error_category"] == "provider_error"
    assert scheduled == []


def test_recoverable_returned_failure_stops_after_three_attempts(tmp_path, monkeypatch) -> None:
    payload = {
        "status": "failed",
        "data_quality": "invalid",
        "message": "rpc fail (code=1001)",
        "error": {"category": "provider_error", "api_code": "1001"},
    }
    db_path, job_id, scheduled = _run_job_against_database(tmp_path, monkeypatch, payload)
    previous_database = app_module.app.config["DATABASE"]
    app_module.app.config["DATABASE"] = str(db_path)
    try:
        for _ in range(2):
            with get_db(str(db_path)) as conn:
                conn.execute(
                    "UPDATE background_jobs SET status = 'pending', next_attempt_at = NULL WHERE id = ?",
                    (job_id,),
                )
                conn.commit()
            app_module.run_background_job(app_module.app, job_id)
    finally:
        app_module.app.config["DATABASE"] = previous_database

    with get_db(str(db_path)) as conn:
        job = conn.execute(
            "SELECT status, attempt_count, next_attempt_at, error_message FROM background_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

    assert job["status"] == "failed"
    assert job["attempt_count"] == 3
    assert job["next_attempt_at"] is None
    assert "Limite de tentativas" in job["error_message"]
    assert len(scheduled) == 2


def test_sigenergy_daily_completion_does_not_require_a_second_fact_shape(tmp_path) -> None:
    db_path = tmp_path / "daily-only-completion.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = int(
            conn.execute("INSERT INTO assets (project_name) VALUES ('Daily totals')").lastrowid
        )
        fact = parse_sigenergy_daily_history(
            {
                "powerGenerationKwh": 0,
                "powerUseKwh": 0,
                "powerOneselfKwh": 0,
                "powerSelfConsumptionKwh": 0,
                "powerToGridKwh": 0,
                "powerFromGridKwh": 0,
            },
            system_id="SIG-DAILY",
            period_date=date(2026, 7, 1),
            confirmed_unit="kWh",
        )
        persist_sigenergy_daily_history(conn, asset_id=asset_id, fact=fact)
        conn.execute("DELETE FROM energy_interval_facts")
        conn.commit()

        assert history_day_is_complete(
            conn,
            asset_id=asset_id,
            external_id="SIG-DAILY",
            target_date="2026-07-01",
        )
