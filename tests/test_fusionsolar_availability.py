from __future__ import annotations

import json

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.services.fusionsolar import classify_fusionsolar_inverter_availability


def test_inverter_state_classification() -> None:
    assert classify_fusionsolar_inverter_availability({"inverter_state": 512}) == "available"
    assert classify_fusionsolar_inverter_availability({"inverter_state": 513}) == "available"
    assert classify_fusionsolar_inverter_availability({"inverter_state": 768}) == "unavailable"
    assert classify_fusionsolar_inverter_availability({"inverter_state": 771}) == "unavailable"
    assert classify_fusionsolar_inverter_availability({}) == "unknown"
    assert classify_fusionsolar_inverter_availability({"inverter_state": 512}, has_recent_data=False) == "no_communication"


def test_calculate_asset_availability_handles_capacity_and_missing_power() -> None:
    summary = app_module.calculate_asset_availability(
        [
            {"enabled": 1, "availability_status": "available", "communication_status": "recent", "rated_power_kw": 100},
            {"enabled": 1, "availability_status": "available", "communication_status": "recent", "rated_power_kw": 100},
            {"enabled": 1, "availability_status": "unavailable", "communication_status": "recent", "rated_power_kw": 50},
        ]
    )
    assert summary["capacity_availability_pct"] == 80.0
    assert summary["affected_power_kw"] == 50

    missing = app_module.calculate_asset_availability(
        [
            {"enabled": 1, "availability_status": "available", "communication_status": "recent", "rated_power_kw": 100},
            {"enabled": 1, "availability_status": "unavailable", "communication_status": "recent", "rated_power_kw": None},
        ]
    )
    assert missing["capacity_availability_pct"] is None
    assert missing["affected_power_kw"] is None


def test_availability_schema_is_created(tmp_path) -> None:
    db_path = tmp_path / "availability.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert {"provider_devices", "device_realtime_snapshots", "availability_daily"} <= tables
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
        assert "idx_provider_devices_provider_station" in indexes
        assert "idx_device_realtime_snapshots_asset_collected" in indexes
        assert "idx_availability_daily_asset_provider_period" in indexes
    finally:
        conn.close()


def test_parse_fusionsolar_pv_inputs_handles_present_and_missing_fields() -> None:
    currents, voltages = app_module.parse_fusionsolar_pv_inputs(
        {"dataItemMap": {"pv1_i": 1.2, "pv2_i": 2.3, "pv1_u": 400, "pv2_u": 401}}
    )
    assert currents == {"pv1_i": 1.2, "pv2_i": 2.3}
    assert voltages == {"pv1_u": 400, "pv2_u": 401}
    assert app_module.parse_fusionsolar_pv_inputs({}) == ({}, {})


def test_calculate_pv_input_health_uses_expected_inputs_and_voltage() -> None:
    health = app_module.calculate_pv_input_health(
        {"pv1_i": 4.2, "pv2_i": 0.0, "pv3_i": 3.9},
        {"pv1_u": 600, "pv2_u": 0.0, "pv3_u": 600},
        expected_string_indexes={1, 2, 3},
    )
    assert health["available_strings"] == 2
    assert health["total_strings"] == 3
    assert health["unavailable_strings"] == 1
    assert health["string_availability_pct"] == 66.67


def test_learn_expected_strings_requires_two_voltage_observations(tmp_path) -> None:
    db_path = tmp_path / "strings.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        asset_id = int(conn.execute("INSERT INTO assets (project_name) VALUES ('Plant A')").lastrowid)
        device_id = int(
            conn.execute(
                """
                INSERT INTO provider_devices (
                    asset_id, provider, station_code, external_device_id, created_at, updated_at
                ) VALUES (?, 'FusionSolar', 'S1', 'D1', '2026-05-18T10:00:00', '2026-05-18T10:00:00')
                """,
                (asset_id,),
            ).lastrowid
        )
        first = app_module.learn_expected_strings_from_voltage(conn, device_id, {"pv1_u": 600}, "2026-05-18T10:00:00")
        second = app_module.learn_expected_strings_from_voltage(conn, device_id, {"pv1_u": 610}, "2026-05-18T10:05:00")
        assert first == set()
        assert second == {1}
    finally:
        conn.close()


def test_device_availability_sync_inserts_devices_snapshots_and_daily_summary(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "availability-sync.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    now = app_module.datetime.now().isoformat(timespec="seconds")
    try:
        asset_id = int(conn.execute("INSERT INTO assets (project_name) VALUES ('Plant A')").lastrowid)
        conn.execute(
            """
            INSERT INTO integration_configs (
                provider, username, password, base_url, enabled, created_at, updated_at
            ) VALUES ('FusionSolar', 'user', 'secret', 'https://example.test', 1, ?, ?)
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
        conn.commit()

        monkeypatch.setattr(app_module, "get_fusionsolar_session", lambda *_args, **_kwargs: (object(), "token"))
        monkeypatch.setattr(
            app_module,
            "fetch_fusionsolar_device_list",
            lambda *_args, **_kwargs: [
                {"stationCode": "S1", "devId": "D1", "devTypeId": 1, "devName": "INV-1", "ratedPower": 100},
            ],
        )
        monkeypatch.setattr(
            app_module,
            "fetch_fusionsolar_device_realtime_map",
            lambda *_args, **_kwargs: {
                "D1": {"devId": "D1", "dataItemMap": {"inverter_state": 512, "active_power": 50, "pv1_i": 1.1}}
            },
        )

        result = app_module.run_fusionsolar_device_availability_sync(conn, "FusionSolar")

        assert result["devices"] == 1
        assert result["snapshots"] == 1
        assert result["assets"] == 1
        assert result["sampled_days_recalculated_locally"] == 1
        assert result["sampled_states"] == {"indeterminate": 1}
        assert conn.execute("SELECT COUNT(*) AS total FROM provider_devices").fetchone()["total"] == 1
        snapshot = conn.execute("SELECT * FROM device_realtime_snapshots").fetchone()
        assert snapshot["availability_status"] == "available"
        assert json.loads(snapshot["pv_current_json"]) == {"pv1_i": 1.1}
        daily = conn.execute("SELECT * FROM availability_daily").fetchone()
        assert daily["inverter_availability_pct"] == 100.0
    finally:
        conn.close()
