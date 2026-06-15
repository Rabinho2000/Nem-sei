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


def test_performance_page_hides_legacy_availability_ui(tmp_path) -> None:
    db_path = tmp_path / "performance-list.db"
    ensure_database(str(db_path))
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
    assert b"WAT" in response.data
    assert "Disponibilidade por central".encode("utf-8") not in response.data
    assert b"Disponibilidade strings" not in response.data
    assert b"Sem comunicacao" not in response.data
    assert b"Producao kWh" not in response.data


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
