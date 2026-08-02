import sqlite3

from monitoring_board.app_factory import get_asset_operational_status


def test_sigenergy_operational_status_uses_latest_technical_state() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE asset_integrations (id INTEGER, asset_id INTEGER, provider TEXT, external_id TEXT, enabled INTEGER, last_status TEXT, last_sync_at TEXT)")
    conn.execute("CREATE TABLE provider_system_inventory (provider TEXT, external_id TEXT, operational_status TEXT, last_state_at TEXT, last_snapshot_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO asset_integrations VALUES (1, 1, 'Sigenergy', 'SIG-1', 1, '', '2026-08-01T10:00:00')")
    conn.execute("INSERT INTO provider_system_inventory VALUES ('Sigenergy', 'SIG-1', 'disconnected', '2026-08-01T11:00:00', '', '2026-08-01T11:00:00')")
    assert get_asset_operational_status(conn, 1) == {"status": "Desconectada", "observed_at": "2026-08-01T11:00:00", "provider": "Sigenergy"}


def test_missing_technical_state_is_not_reported_as_disconnected() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE asset_integrations (id INTEGER, asset_id INTEGER, provider TEXT, external_id TEXT, enabled INTEGER, last_status TEXT, last_sync_at TEXT)")
    conn.execute("CREATE TABLE provider_system_inventory (provider TEXT, external_id TEXT, operational_status TEXT, last_state_at TEXT, last_snapshot_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO asset_integrations VALUES (1, 1, 'Sigenergy', 'SIG-1', 1, '', '')")
    assert get_asset_operational_status(conn, 1)["status"] == "Sem dados"
