from __future__ import annotations

from datetime import date, datetime, timedelta

import app as app_module
import pytest
from monitoring_board.db import get_db
from monitoring_board.services.api_rate_limit import ApiRateLimitError


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

    assert result["status"] == "failed"
    assert result["legacy_status"] == "forbidden"
    assert inventory["SIG-FORBIDDEN"]["data_quality"] == "missing"
    assert inventory["SIG-FORBIDDEN"]["last_error"] in (None, "")
    assert inventory["SIG-OTHER"]["last_error"] in (None, "")
    assert fact_count == 0
    with get_db(str(db_path)) as conn:
        scoped = conn.execute(
            """
            SELECT operation, external_id, status, http_status
            FROM provider_operation_state
            WHERE provider = 'Sigenergy'
              AND operation = 'history'
              AND external_id = 'SIG-FORBIDDEN'
            """
        ).fetchone()
    assert dict(scoped) == {
        "operation": "history",
        "external_id": "SIG-FORBIDDEN",
        "status": "failed",
        "http_status": 403,
    }


def test_daily_history_rate_limit_is_scoped_and_surfaces_cooldown(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "sigenergy-energy-rate-limit.db"
    app_module.ensure_database(str(db_path))
    cooldown = datetime.now() + timedelta(minutes=30)

    class RateLimitedClient:
        def __init__(self, _config, session=None) -> None:
            self.session = session

        def get_system_history(self, _system_id, *, level, target_date):
            raise ApiRateLimitError(
                "Sigenergy",
                "production",
                cooldown,
                "history limited",
            )

    monkeypatch.setattr(
        app_module.sigenergy_service,
        "SigenergyClient",
        RateLimitedClient,
    )
    with get_db(str(db_path)) as conn:
        configure_sigenergy_asset(conn, external_id="SIG-LIMITED")
        with pytest.raises(ApiRateLimitError) as error:
            app_module.run_sigenergy_daily_energy_sync(
                conn,
                external_id="SIG-LIMITED",
                target_date=date(2020, 1, 2),
            )
        scoped = conn.execute(
            """
            SELECT operation, external_id, status, http_status
            FROM provider_operation_state
            WHERE provider = 'Sigenergy'
              AND operation = 'history'
              AND external_id = 'SIG-LIMITED'
            """
        ).fetchone()
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM energy_interval_facts"
        ).fetchone()[0]

    assert error.value.area == "production"
    assert error.value.cooldown_until == cooldown
    assert dict(scoped) == {
        "operation": "history",
        "external_id": "SIG-LIMITED",
        "status": "rate_limited",
        "http_status": 429,
    }
    assert fact_count == 0
