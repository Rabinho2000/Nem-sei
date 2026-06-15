from __future__ import annotations

import app as app_module
from monitoring_board.db import get_db


def test_fusionsolar_minor_alarm_is_alert_not_error() -> None:
    row = app_module.normalize_fusionsolar_plant_row(
        {"plantCode": "S1", "plantName": "Plant A"},
        {"dataItemMap": {"real_health_state": "2"}},
        [{"lev": 3, "alarmName": "Minor alarm"}],
    )

    assert row["status"] == "Alerta"
    assert row["alarm_levels"] == "3"


def test_fusionsolar_major_alarm_is_error() -> None:
    row = app_module.normalize_fusionsolar_plant_row(
        {"plantCode": "S1", "plantName": "Plant A"},
        {"dataItemMap": {"real_health_state": "2"}},
        [{"lev": 2, "alarmName": "Major alarm"}],
    )

    assert row["status"] == "Erro"


def test_fusionsolar_sync_records_recovery_after_same_day_operational(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "fusionsolar-sync.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    today = app_module.date.today().isoformat()
    now = app_module.datetime.now().isoformat(timespec="seconds")
    try:
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name, active_contract) VALUES (?, 'yes')",
                ("Plant A",),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO integration_configs (provider, enabled, created_at, updated_at)
            VALUES ('FusionSolar', 1, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
            VALUES (?, 'FusionSolar', 'S1', 'Plant A', 1)
            """,
            (asset_id,),
        )
        conn.execute(
            """
            INSERT INTO monitoring_records (asset_id, status, record_date, notes, source)
            VALUES (?, 'Operacional', ?, 'morning sync', 'FusionSolar')
            """,
            (asset_id, today),
        )
        conn.execute(
            """
            INSERT INTO monitoring_records (asset_id, status, record_date, notes, source)
            VALUES (?, 'Erro', ?, 'afternoon sync', 'FusionSolar')
            """,
            (asset_id, today),
        )
        conn.commit()

        def fake_fusionsolar_check(_conn, _provider, dry_run=False):
            return {
                "rows": [
                    {
                        "external_id": "S1",
                        "external_name": "Plant A",
                        "status": "Operacional",
                        "notes": "health_state=healthy",
                        "payload": {},
                    }
                ],
                "station_count": 1,
                "realtime_count": 1,
                "alarm_count": 0,
                "alarm_error": "",
            }

        monkeypatch.setattr(app_module, "run_fusionsolar_check", fake_fusionsolar_check)

        app_module.run_fusionsolar_sync(conn, "FusionSolar", trigger_type="manual")

        latest = conn.execute("SELECT * FROM latest_monitoring_view WHERE asset_id = ?", (asset_id,)).fetchone()
        assert latest["status"] == "Operacional"
        assert (
            conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM monitoring_records
                WHERE asset_id = ? AND record_date = ? AND status = 'Operacional' AND source = 'FusionSolar'
                """,
                (asset_id, today),
            ).fetchone()["total"]
            == 2
        )
    finally:
        conn.close()
