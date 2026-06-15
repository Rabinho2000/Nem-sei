from __future__ import annotations

import app as app_module
from monitoring_board.db import get_db


def make_conn(tmp_path):
    db_path = tmp_path / "audit.db"
    app_module.ensure_database(str(db_path))
    return get_db(str(db_path))


def add_asset(conn, name: str) -> int:
    cursor = conn.execute(
        "INSERT INTO assets (project_name, installation_group) VALUES (?, ?)",
        (name, name),
    )
    return int(cursor.lastrowid)


def add_integration(conn, asset_id: int, external_id: str, external_name: str) -> None:
    conn.execute(
        """
        INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
        VALUES (?, ?, ?, ?, 1)
        """,
        (asset_id, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR, external_id, external_name),
    )


def test_link_audit_warns_when_two_fusionsolar_rows_share_same_asset(tmp_path) -> None:
    conn = make_conn(tmp_path)
    try:
        asset_id = add_asset(conn, "Central Norte")
        add_integration(conn, asset_id, "fs-1", "Central Norte")
        add_integration(conn, asset_id, "fs-2", "Central Norte Bateria")
        conn.commit()

        rows = app_module.get_fusionsolar_link_audit_rows(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        assert len(rows) == 2
        assert {row["duplicate_count"] for row in rows} == {2}
        assert all(row["verdict"] == "Atencao" for row in rows)
        assert all("2 entradas FusionSolar" in row["reason"] for row in rows)
    finally:
        conn.close()


def test_link_audit_shows_active_oem_assets_missing_from_fusionsolar(tmp_path) -> None:
    conn = make_conn(tmp_path)
    try:
        asset_id = add_asset(conn, "AHBV Lagos (EPC)")
        conn.execute("UPDATE assets SET active_contract = 'yes' WHERE id = ?", (asset_id,))
        conn.commit()

        rows = app_module.get_fusionsolar_link_audit_rows(conn, app_module.INTEGRATION_PROVIDER_FUSIONSOLAR)

        missing = [row for row in rows if row["asset_id"] == asset_id]
        assert len(missing) == 1
        assert missing[0]["verdict"] == "Rever"
        assert "sem entrada devolvida pelo FusionSolar" in missing[0]["reason"]
    finally:
        conn.close()
