from __future__ import annotations

from datetime import date

import app as app_module
from app import app as flask_app
from app import ensure_database, upsert_production_record
from monitoring_board.db import get_db


def test_performance_debug_route_renders_payload_safely(tmp_path) -> None:
    db_path = tmp_path / "debug.db"
    ensure_database(str(db_path))
    connection = get_db(str(db_path))
    try:
        cursor = connection.execute("INSERT INTO assets (project_name, kwp) VALUES (?, ?)", ("Central <A>", "50"))
        asset_id = int(cursor.lastrowid)
        upsert_production_record(
            connection,
            asset_id=asset_id,
            provider="FusionSolar",
            external_id="S1",
            period_type="day",
            period_date=date(2026, 5, 3),
            production_kwh=10,
            specific_yield=0.2,
            expected_kwh=None,
            expected_specific_yield=None,
            deviation_pct=None,
            performance_status="Sem referência",
            expected_source="none",
            data_quality="ok",
            notes="",
            payload_json='{"stationCode":"S1","dataItemMap":{"PVYield":"10","unsafe":"<script>alert(1)</script>"}}',
            selected_production_key="PVYield",
            selected_production_raw_value="10",
        )
        record_id = int(connection.execute("SELECT id FROM production_records").fetchone()["id"])
        connection.commit()
    finally:
        connection.close()

    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
        response = client.get(f"/performance/debug/{record_id}")
    finally:
        flask_app.config["DATABASE"] = previous_db

    assert response.status_code == 200
    assert b"PVYield" in response.data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data
    assert b"<script>alert(1)</script>" not in response.data


def test_performance_list_query_uses_availability_fields(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "performance-list.db"
    ensure_database(str(db_path))
    captured_sql = ""

    def fake_query_all(_conn, sql, _params=()):
        nonlocal captured_sql
        captured_sql = sql
        return [
            {
                "asset_id": 1,
                "project_name": "Central A",
                "location": "Lisboa",
                "active_contract": "yes",
                "period_date": "2026-05-05",
                "inverter_availability_pct": 80.0,
                "capacity_availability_pct": 75.0,
                "communication_availability_pct": 100.0,
                "available_inverters": 4,
                "total_inverters": 5,
                "unavailable_inverters": 1,
                "no_communication_devices": 0,
                "string_availability_pct": 80.0,
                "available_strings": 8,
                "total_strings": 10,
                "unavailable_strings": 2,
                "affected_power_kw": 50.0,
                "updated_at": "2026-05-06T10:00:00",
            }
        ]

    monkeypatch.setattr(app_module, "query_all", fake_query_all)
    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
        response = client.get("/performance")
    finally:
        flask_app.config["DATABASE"] = previous_db

    assert response.status_code == 200
    assert b"Central A" in response.data
    assert "Disponibilidade por central".encode("utf-8") in response.data
    assert "Producao kWh".encode("utf-8") not in response.data
    assert "ad.*" not in captured_sql
    assert "payload_json" not in captured_sql
    assert "production_records" not in captured_sql


def test_performance_post_runs_availability_sync(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "performance-job.db"
    ensure_database(str(db_path))
    called: list[str] = []

    def fake_availability_sync(*_args, **_kwargs):
        called.append("sync")
        return {"devices": 2, "assets": 1}

    monkeypatch.setattr(app_module, "run_fusionsolar_device_availability_sync", fake_availability_sync)

    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
            sess["csrf_token"] = "token"
        response = client.post(
            "/performance",
            data={
                "csrf_token": "token",
                "action": "sync_availability",
            },
        )
    finally:
        flask_app.config["DATABASE"] = previous_db

    assert response.status_code == 302
    assert called == ["sync"]


def test_backfill_post_queues_job_without_running_inline(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "backfill-job.db"
    ensure_database(str(db_path))
    scheduled_jobs: list[int] = []

    def fail_inline_backfill(*_args, **_kwargs):
        raise AssertionError("backfill should not run inside the request")

    monkeypatch.setattr(app_module, "run_fusionsolar_production_backfill", fail_inline_backfill)
    monkeypatch.setattr(app_module, "schedule_background_job", lambda _app, job_id: scheduled_jobs.append(job_id) or True)

    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "admin"
            sess["csrf_token"] = "token"
        response = client.post(
            "/performance/backfill",
            data={
                "csrf_token": "token",
                "period_type": "day",
                "from_year": "2025",
                "to_year": "2025",
                "max_api_calls": "1",
            },
        )
    finally:
        flask_app.config["DATABASE"] = previous_db

    conn = get_db(str(db_path))
    try:
        job = conn.execute("SELECT * FROM background_jobs").fetchone()
        assert response.status_code == 302
        assert job["job_type"] == "fusionsolar_production_backfill"
        assert job["status"] == "pending"
        assert scheduled_jobs == [job["id"]]
    finally:
        conn.close()
