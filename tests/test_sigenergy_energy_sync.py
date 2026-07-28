from __future__ import annotations

from datetime import date, datetime

import app as app_module
from monitoring_board.db import get_db


def configure_sigenergy_asset(conn, *, external_id: str) -> int:
    app_module.ensure_integration_seed_data(conn)
    conn.execute(
        """
        UPDATE integration_configs
        SET enabled = 1, username = 'app-key', password = 'app-secret'
        WHERE provider = 'Sigenergy'
        """
    )
    asset_id = int(
        conn.execute(
            "INSERT INTO assets (project_name) VALUES ('Expertcom')"
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO asset_integrations (
            asset_id, provider, external_id, external_name, enabled
        ) VALUES (?, 'Sigenergy', ?, 'Expertcom', 1)
        """,
        (asset_id, external_id),
    )
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO provider_system_inventory (
            provider, external_id, external_name, metadata_json,
            access_status, first_discovered_at, last_discovered_at,
            data_quality, created_at, updated_at
        ) VALUES (
            'Sigenergy', ?, 'Expertcom', '{}', 'accessible', ?, ?,
            'missing', ?, ?
        )
        """,
        (external_id, now, now, now, now),
    )
    conn.commit()
    return asset_id


def test_daily_history_sync_flows_through_queue_into_report_records(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-energy-sync.db"
    app_module.ensure_database(str(db_path))

    class FakeClient:
        def __init__(self, _config, session=None) -> None:
            self.session = session

        def get_system_history(self, system_id, *, level, target_date):
            assert system_id == "SIG-ENERGY"
            assert level == "Day"
            assert target_date == "2020-01-02"
            return {
                "powerGeneration": 10.5,
                "powerToGrid": 2.0,
                "powerSelfConsumption": 8.5,
                "powerUse": 12.0,
                "powerFromGrid": 3.5,
                "powerOneself": 8.5,
                "esCharging": 0.4,
                "esDischarging": 0.2,
            }

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        FakeClient,
    )
    monkeypatch.setenv("SIGENERGY_HISTORY_ENERGY_UNIT", "kWh")
    with get_db(str(db_path)) as conn:
        asset_id = configure_sigenergy_asset(conn, external_id="SIG-ENERGY")

        result = app_module.run_sigenergy_daily_energy_sync(
            conn,
            external_id="SIG-ENERGY",
            target_date=date(2020, 1, 2),
        )
        fact = conn.execute(
            "SELECT * FROM energy_interval_facts WHERE external_id = 'SIG-ENERGY'"
        ).fetchone()
        daily = conn.execute(
            """
            SELECT *
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'day' AND period_date = '2020-01-02'
            """,
            (asset_id,),
        ).fetchone()
        queue = conn.execute(
            """
            SELECT daily_call_count
            FROM production_api_queue_state
            WHERE provider = 'sigenergy' AND api_area = 'production'
            """
        ).fetchone()

    assert result["status"] == "success"
    assert fact["production_kwh"] == 10.5
    assert fact["data_quality"] == "complete"
    assert daily["production_kwh"] == 10.5
    assert daily["grid_import_kwh"] == 3.5
    assert queue["daily_call_count"] == 1


def test_daily_history_403_isolated_to_target_installation(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-energy-403.db"
    app_module.ensure_database(str(db_path))

    class ForbiddenClient:
        def __init__(self, _config, session=None) -> None:
            self.session = session

        def get_system_history(self, _system_id, *, level, target_date):
            raise app_module.sigenergy_service.SigenergyAPIError(
                "forbidden",
                status_code=403,
            )

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        ForbiddenClient,
    )
    with get_db(str(db_path)) as conn:
        configure_sigenergy_asset(conn, external_id="SIG-FORBIDDEN")
        configure_sigenergy_asset(conn, external_id="SIG-OTHER")
        result = app_module.run_sigenergy_daily_energy_sync(
            conn,
            external_id="SIG-FORBIDDEN",
            target_date=date(2020, 1, 2),
        )
        inventory = {
            row["external_id"]: dict(row)
            for row in conn.execute(
                """
                SELECT external_id, data_quality, last_error
                FROM provider_system_inventory
                ORDER BY external_id
                """
            ).fetchall()
        }
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM energy_interval_facts"
        ).fetchone()[0]

    assert result["status"] == "forbidden"
    assert inventory["SIG-FORBIDDEN"]["data_quality"] == "missing"
    assert "HTTP 403" in inventory["SIG-FORBIDDEN"]["last_error"]
    assert inventory["SIG-OTHER"]["last_error"] in (None, "")
    assert fact_count == 0
