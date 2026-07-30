from __future__ import annotations

import sqlite3

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.services.sigenergy_contracts import (
    OPERATION_LEGACY_UNKNOWN,
)


NEW_INVENTORY_COLUMNS = {
    "operational_status",
    "sync_status",
    "last_sync_at",
    "last_snapshot_at",
    "first_access_at",
    "last_access_at",
}


def test_sigenergy_schema_upgrade_is_additive_idempotent_and_backup_rollbackable(
    tmp_path,
) -> None:
    db_path = tmp_path / "legacy-production-copy.db"
    rollback_path = tmp_path / "legacy-production-copy.rollback.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        app_module.ensure_integration_seed_data(conn)
        conn.execute(
            """
            INSERT INTO provider_system_inventory (
                provider, external_id, external_name, metadata_json,
                access_status, validation_method, first_discovered_at,
                last_discovered_at, data_quality, last_error, created_at,
                updated_at
            ) VALUES (
                'Sigenergy', '25062000156', 'Legacy', '{}', 'accessible',
                'discovery', '2026-07-01T00:00:00',
                '2026-07-29T00:00:00', 'missing', '',
                '2026-07-01T00:00:00', '2026-07-29T00:00:00'
            )
            """
        )
        conn.execute(
            """
            UPDATE integration_configs
            SET last_error =
                '/openapi/systems/25062000156/energyFlow failed',
                updated_at = '2026-07-29T12:00:00'
            WHERE provider = 'Sigenergy'
            """
        )
        conn.execute(
            """
            DELETE FROM app_state
            WHERE key = 'sigenergy_scoped_legacy_error_v1'
            """
        )
        conn.execute("DROP INDEX IF EXISTS idx_sigenergy_inventory_sync_status")
        conn.execute("DROP TABLE IF EXISTS provider_operation_events")
        conn.execute("DROP TABLE IF EXISTS provider_operation_state")
        for column in NEW_INVENTORY_COLUMNS:
            conn.execute(
                f"ALTER TABLE provider_system_inventory DROP COLUMN {column}"
            )
        conn.commit()

    with sqlite3.connect(db_path) as source, sqlite3.connect(
        rollback_path
    ) as destination:
        source.backup(destination)

    app_module.ensure_database(str(db_path))
    app_module.ensure_database(str(db_path))

    with get_db(str(db_path)) as conn:
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(provider_system_inventory)"
            )
        }
        inventory = conn.execute(
            """
            SELECT external_name, operational_status, sync_status
            FROM provider_system_inventory
            WHERE provider = 'Sigenergy' AND external_id = '25062000156'
            """
        ).fetchone()
        migrated_error = conn.execute(
            """
            SELECT operation, external_id, status, message
            FROM provider_operation_state
            WHERE provider = 'Sigenergy'
              AND operation = ?
              AND external_id = '25062000156'
            """,
            (OPERATION_LEGACY_UNKNOWN,),
        ).fetchone()
        event_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM provider_operation_events
            WHERE provider = 'Sigenergy'
              AND operation = ?
              AND external_id = '25062000156'
            """,
            (OPERATION_LEGACY_UNKNOWN,),
        ).fetchone()[0]
        legacy_global_error = conn.execute(
            """
            SELECT last_error
            FROM integration_configs
            WHERE provider = 'Sigenergy'
            """
        ).fetchone()[0]
        operation_state_pk = {
            row["name"]: int(row["pk"])
            for row in conn.execute(
                "PRAGMA table_info(provider_operation_state)"
            )
            if int(row["pk"])
        }
        operation_indexes = {
            row["name"]
            for row in conn.execute(
                "PRAGMA index_list(provider_operation_events)"
            )
        }
        mapping_unique_columns = {
            tuple(
                column["name"]
                for column in conn.execute(
                    f"PRAGMA index_info({index['name']})"
                )
            )
            for index in conn.execute(
                "PRAGMA index_list(asset_integrations)"
            )
            if int(index["unique"])
        }
        foreign_key_violations = conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert NEW_INVENTORY_COLUMNS <= columns
    assert inventory["external_name"] == "Legacy"
    assert inventory["operational_status"] == "unknown"
    assert inventory["sync_status"] == "never_synced"
    assert migrated_error["status"] == "error"
    assert "25062000156" in migrated_error["message"]
    assert event_count == 1
    assert legacy_global_error == ""
    assert operation_state_pk == {
        "provider": 1,
        "operation": 2,
        "external_id": 3,
    }
    assert "idx_provider_operation_events_scope" in operation_indexes
    assert ("provider", "external_id") in mapping_unique_columns
    assert foreign_key_violations == []

    with sqlite3.connect(rollback_path) as rollback:
        rollback_columns = {
            row[1]
            for row in rollback.execute(
                "PRAGMA table_info(provider_system_inventory)"
            )
        }
        preserved = rollback.execute(
            """
            SELECT external_name
            FROM provider_system_inventory
            WHERE provider = 'Sigenergy' AND external_id = '25062000156'
            """
        ).fetchone()
        operation_tables = rollback.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'table'
              AND name IN (
                  'provider_operation_state',
                  'provider_operation_events'
              )
            """
        ).fetchone()[0]

    assert not (NEW_INVENTORY_COLUMNS & rollback_columns)
    assert preserved == ("Legacy",)
    assert operation_tables == 0
