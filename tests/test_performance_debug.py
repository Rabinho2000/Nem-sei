from __future__ import annotations

from datetime import date

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
