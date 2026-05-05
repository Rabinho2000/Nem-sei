from __future__ import annotations

from datetime import datetime, timedelta

import app as app_module
from monitoring_board.db import get_db, query_scalar


def make_conn(tmp_path):
    db_path = tmp_path / "alerts.db"
    app_module.ensure_database(str(db_path))
    return get_db(str(db_path))


def add_asset(conn, *, name="Central A", maintenance="yes", active_contract="yes", monitoring_status="active"):
    cursor = conn.execute(
        """
        INSERT INTO assets (
            project_name, maintenance, active_contract, monitoring_enabled, alerts_enabled, monitoring_status, selected_for_alerts
        ) VALUES (?, ?, ?, 1, 1, ?, 0)
        """,
        (name, maintenance, active_contract, monitoring_status),
    )
    return int(cursor.lastrowid)


def configure_alerts(monkeypatch, conn):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_ALERTS_ENABLED", "true")
    app_module.set_alert_setting(conn, "TELEGRAM_ALERTS_ENABLED", "true")
    app_module.set_alert_setting(conn, "ALERT_SCOPE", "only_o&m")
    app_module.set_alert_setting(conn, "SEND_RECURRENT_ALERTS", "false")


def test_non_oem_asset_is_blocked_for_only_oem_scope(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn, maintenance="no")
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    allowed, reason = app_module.alert_decision(conn, asset, "novo_erro", "a", datetime.now())

    assert not allowed
    assert reason == "out_of_scope"


def test_blacklisted_asset_is_blocked(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn)
    conn.execute(
        "INSERT INTO alert_blacklist (asset_id, asset_name, created_at, active) VALUES (?, ?, ?, 1)",
        (asset_id, "Central A", datetime.now().isoformat(timespec="seconds")),
    )
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    allowed, reason = app_module.alert_decision(conn, asset, "novo_erro", "a", datetime.now())

    assert not allowed
    assert reason == "blacklist"


def test_silenced_asset_is_blocked(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn, monitoring_status="silenced")
    conn.execute(
        "UPDATE assets SET silenced_until = ? WHERE id = ?",
        ((datetime.now() + timedelta(hours=1)).isoformat(timespec="minutes"), asset_id),
    )
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    allowed, reason = app_module.alert_decision(conn, asset, "novo_erro", "a", datetime.now())

    assert not allowed
    assert reason == "silenced"


def test_out_of_scope_asset_is_blocked(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn, monitoring_status="out_of_scope")
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    allowed, reason = app_module.alert_decision(conn, asset, "novo_erro", "a", datetime.now())

    assert not allowed
    assert reason == "out_of_scope"


def test_active_oem_asset_allows_new_error(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn)
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    allowed, reason = app_module.alert_decision(conn, asset, "novo_erro", "a", datetime.now())

    assert allowed
    assert reason == ""


def test_recurrent_alert_setting_blocks_recurrent(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn)
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    allowed, reason = app_module.alert_decision(conn, asset, "recorrente_7d", "a", datetime.now())

    assert not allowed
    assert reason == "alert_type_disabled"


def test_baseline_excludes_old_recurrences(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn)
    old_date = (datetime.now() - timedelta(days=3)).date().isoformat()
    today = datetime.now().date().isoformat()
    for index in range(3):
        conn.execute(
            "INSERT INTO monitoring_records (asset_id, status, record_date, source) VALUES (?, 'Erro', ?, 'test')",
            (asset_id, old_date),
        )
    app_module.set_alert_setting(conn, "ALERT_BASELINE_AT", datetime.now().isoformat(timespec="seconds"))

    assert app_module.count_problem_occurrences_since(conn, asset_id, today) == 0


def test_setting_baseline_updates_timestamp(tmp_path):
    conn = make_conn(tmp_path)
    baseline_at = datetime.now().isoformat(timespec="seconds")
    app_module.set_alert_setting(conn, "ALERT_BASELINE_AT", baseline_at)

    assert app_module.get_alert_setting(conn, "ALERT_BASELINE_AT") == baseline_at


def test_duplicate_alert_key_does_not_send_again(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    asset_id = add_asset(conn)
    app_module.record_telegram_alert(conn, asset_id, "novo_erro", "same-key", "msg", "sent")
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    allowed, reason = app_module.alert_decision(conn, asset, "novo_erro", "same-key", datetime.now())

    assert not allowed
    assert reason == "cooldown"


def test_more_than_10_alerts_are_aggregated(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    configure_alerts(monkeypatch, conn)
    monkeypatch.setattr(app_module, "send_telegram_message", lambda message: True)
    events = []
    for index in range(11):
        asset_id = add_asset(conn, name=f"Central {index}")
        events.append(
            {
                "asset_id": asset_id,
                "project_name": f"Central {index}",
                "previous_status": "Operacional",
                "current_status": "Erro",
                "happened_at": datetime.now().isoformat(timespec="seconds"),
                "alert_type": "novo_erro",
            }
        )

    app_module.process_monitoring_alerts(conn, events, 99, datetime.now())

    assert query_scalar(conn, "SELECT COUNT(*) FROM telegram_alerts WHERE alert_type = 'batch_many_alerts' AND status = 'sent'") == 1
    assert query_scalar(conn, "SELECT COUNT(*) FROM telegram_alerts WHERE blocked_reason = 'batch_aggregated'") == 11


def test_fusionsolar_alarm_summary_includes_alarm_type_and_device():
    summary = app_module.summarize_fusionsolar_alarms(
        [
            {
                "alarmName": "Grid Loss",
                "devName": "Inverter 01",
                "lev": "major",
                "raiseTime": "1714200000000",
            }
        ]
    )

    assert summary["primary_alarm_name"] == "Grid Loss"
    assert summary["primary_alarm_device"] == "Inverter 01"
    assert "Grid Loss @ Inverter 01" in summary["alarm_summary"]
