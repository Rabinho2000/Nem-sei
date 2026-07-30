from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from monitoring_board.services.sigenergy_contracts import (
    AccessStatus,
    DataQuality,
    MappingStatus,
    OPERATION_LEGACY_UNKNOWN,
    OperationalStatus,
    SIGENERGY_PROVIDER,
    ScopedProviderError,
    SyncStatus,
)
from monitoring_board.services.sigenergy_models import (
    normalize_system,
    sanitize_payload,
    sanitize_sigenergy_error,
)


_LEGACY_SYSTEM_PATH = re.compile(
    r"/(?:openapi/)?systems/([A-Za-z0-9_-]{1,64})/",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(sanitize_payload(value), ensure_ascii=True, sort_keys=True)


def ensure_sigenergy_repository_schema(conn: sqlite3.Connection) -> None:
    _ensure_column(
        conn,
        "provider_system_inventory",
        "operational_status TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_column(
        conn,
        "provider_system_inventory",
        "sync_status TEXT NOT NULL DEFAULT 'never_synced'",
    )
    _ensure_column(conn, "provider_system_inventory", "last_sync_at TEXT")
    _ensure_column(conn, "provider_system_inventory", "last_snapshot_at TEXT")
    _ensure_column(conn, "provider_system_inventory", "first_access_at TEXT")
    _ensure_column(conn, "provider_system_inventory", "last_access_at TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS provider_operation_state (
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            http_status INTEGER,
            api_code TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            attempted_at TEXT NOT NULL,
            succeeded_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, operation, external_id)
        );

        CREATE TABLE IF NOT EXISTS provider_operation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            http_status INTEGER,
            api_code TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_provider_operation_events_scope
            ON provider_operation_events(
                provider, operation, external_id, occurred_at DESC
            );
        CREATE INDEX IF NOT EXISTS idx_provider_operation_state_status
            ON provider_operation_state(provider, operation, status);
        CREATE INDEX IF NOT EXISTS idx_sigenergy_inventory_sync_status
            ON provider_system_inventory(
                provider, access_status, sync_status, external_id
            );
        """
    )


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    definition: str,
) -> None:
    column = definition.split()[0]
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def record_operation_result(
    conn: sqlite3.Connection,
    *,
    operation: str,
    status: str,
    external_id: str = "",
    occurred_at: str | None = None,
    message: str = "",
    http_status: int | None = None,
    api_code: str = "",
    metadata: dict[str, Any] | None = None,
    succeeded: bool = False,
    event_status: str | None = None,
) -> None:
    ensure_sigenergy_repository_schema(conn)
    timestamp = occurred_at or _now()
    safe_message = sanitize_sigenergy_error(message)[:2000]
    safe_metadata = _json(metadata or {})
    normalized_external_id = str(external_id or "").strip()
    conn.execute(
        """
        INSERT INTO provider_operation_events (
            provider, operation, external_id, status, http_status, api_code,
            message, metadata_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SIGENERGY_PROVIDER,
            operation,
            normalized_external_id,
            event_status or status,
            http_status,
            str(api_code or ""),
            safe_message,
            safe_metadata,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO provider_operation_state (
            provider, operation, external_id, status, http_status, api_code,
            message, attempted_at, succeeded_at, metadata_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, operation, external_id) DO UPDATE SET
            status = excluded.status,
            http_status = excluded.http_status,
            api_code = excluded.api_code,
            message = excluded.message,
            attempted_at = excluded.attempted_at,
            succeeded_at = CASE
                WHEN excluded.succeeded_at IS NOT NULL
                    THEN excluded.succeeded_at
                ELSE provider_operation_state.succeeded_at
            END,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            SIGENERGY_PROVIDER,
            operation,
            normalized_external_id,
            status,
            http_status,
            str(api_code or ""),
            safe_message,
            timestamp,
            timestamp if succeeded else None,
            safe_metadata,
            timestamp,
        ),
    )


def record_scoped_error(
    conn: sqlite3.Connection,
    error: ScopedProviderError,
    *,
    metadata: dict[str, Any] | None = None,
    state_status: str | None = None,
) -> None:
    record_operation_result(
        conn,
        operation=error.operation,
        external_id=error.external_id,
        status=state_status or error.category,
        occurred_at=error.occurred_at,
        message=error.message,
        http_status=error.http_status,
        api_code=error.api_code,
        metadata=metadata,
        succeeded=False,
        event_status=error.category,
    )


def get_operation_state(
    conn: sqlite3.Connection,
    *,
    operation: str,
    external_id: str = "",
) -> dict[str, Any] | None:
    ensure_sigenergy_repository_schema(conn)
    row = conn.execute(
        """
        SELECT *
        FROM provider_operation_state
        WHERE provider = ? AND operation = ? AND external_id = ?
        """,
        (SIGENERGY_PROVIDER, operation, str(external_id or "").strip()),
    ).fetchone()
    return dict(row) if row is not None else None


def list_latest_scoped_errors(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    ensure_sigenergy_repository_schema(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM provider_operation_state
        WHERE provider = ?
          AND external_id != ''
          AND status NOT IN ('success', 'accessible', 'authenticated')
        ORDER BY attempted_at DESC
        """,
        (SIGENERGY_PROVIDER,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        external_id = str(row["external_id"])
        result.setdefault(external_id, dict(row))
    return result


def migrate_legacy_global_error(conn: sqlite3.Connection) -> bool:
    ensure_sigenergy_repository_schema(conn)
    migration_key = "sigenergy_scoped_legacy_error_v1"
    migrated = conn.execute(
        "SELECT value FROM app_state WHERE key = ?",
        (migration_key,),
    ).fetchone()
    if migrated is not None:
        return False
    row = conn.execute(
        """
        SELECT last_error, updated_at
        FROM integration_configs
        WHERE provider = ?
        """,
        (SIGENERGY_PROVIDER,),
    ).fetchone()
    legacy_error = sanitize_sigenergy_error(
        row["last_error"] if row is not None else ""
    )
    if legacy_error:
        match = _LEGACY_SYSTEM_PATH.search(legacy_error)
        external_id = match.group(1) if match else ""
        record_operation_result(
            conn,
            operation=OPERATION_LEGACY_UNKNOWN,
            external_id=external_id,
            status="error",
            occurred_at=(
                str(row["updated_at"])
                if row is not None and row["updated_at"]
                else _now()
            ),
            message=legacy_error,
            metadata={
                "legacy_source": "integration_configs.last_error",
                "scope_inferred_from_path": bool(match),
            },
        )
        conn.execute(
            """
            UPDATE integration_configs
            SET last_error = ''
            WHERE provider = ?
            """,
            (SIGENERGY_PROVIDER,),
        )
    conn.execute(
        """
        INSERT OR REPLACE INTO app_state (key, value, updated_at)
        VALUES (?, 'done', ?)
        """,
        (migration_key, _now()),
    )
    return bool(legacy_error)


def upsert_discovered_systems(
    conn: sqlite3.Connection,
    systems: Iterable[dict[str, Any]],
    *,
    discovered_at: str,
) -> list[str]:
    discovered_ids: list[str] = []
    for system in systems:
        normalized = normalize_system(system)
        external_id = str(normalized["external_id"])
        discovered_ids.append(external_id)
        conn.execute(
            """
            INSERT INTO provider_system_inventory (
                provider, external_id, external_name, metadata_json,
                access_status, validation_method, first_discovered_at,
                last_discovered_at, last_state, operational_status,
                sync_status, data_quality, last_error, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, 'accessible', 'discovery', ?, ?, ?, ?,
                'never_synced', 'missing', '', ?, ?
            )
            ON CONFLICT(provider, external_id) DO UPDATE SET
                external_name = CASE
                    WHEN excluded.external_name != excluded.external_id
                        THEN excluded.external_name
                    ELSE provider_system_inventory.external_name
                END,
                metadata_json = excluded.metadata_json,
                access_status = 'accessible',
                validation_method = CASE
                    WHEN provider_system_inventory.validation_method =
                         'direct_energy_flow'
                        THEN provider_system_inventory.validation_method
                    ELSE 'discovery'
                END,
                last_discovered_at = excluded.last_discovered_at,
                last_state = CASE
                    WHEN excluded.last_state NOT IN ('', 'Sem dados')
                        THEN excluded.last_state
                    ELSE provider_system_inventory.last_state
                END,
                operational_status = CASE
                    WHEN excluded.operational_status != 'unknown'
                        THEN excluded.operational_status
                    ELSE provider_system_inventory.operational_status
                END,
                updated_at = excluded.updated_at
            """,
            (
                SIGENERGY_PROVIDER,
                external_id,
                normalized["external_name"],
                _json(system),
                discovered_at,
                discovered_at,
                normalized["normalized_status"],
                _operational_status_value(normalized.get("raw_status")),
                discovered_at,
                discovered_at,
            ),
        )
    conn.execute(
        """
        UPDATE integration_configs
        SET last_discovery_at = ?, updated_at = ?
        WHERE provider = ?
        """,
        (discovered_at, discovered_at, SIGENERGY_PROVIDER),
    )
    return discovered_ids


def _operational_status_value(raw_status: Any) -> str:
    from monitoring_board.services.sigenergy_contracts import (
        normalize_operational_status,
    )

    return normalize_operational_status(raw_status).value


def inventory_identity(
    conn: sqlite3.Connection,
    external_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM provider_system_inventory
        WHERE provider = ? AND external_id = ?
        """,
        (SIGENERGY_PROVIDER, external_id),
    ).fetchone()
    return dict(row) if row is not None else None


def mapping_for_system(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    require_enabled: bool = True,
) -> dict[str, Any] | None:
    enabled_clause = "AND ai.enabled = 1" if require_enabled else ""
    row = conn.execute(
        f"""
        SELECT
            ai.*,
            asset.project_name,
            asset.monitoring_status AS asset_monitoring_status
        FROM asset_integrations ai
        JOIN assets asset ON asset.id = ai.asset_id
        WHERE ai.provider = ? AND ai.external_id = ?
          {enabled_clause}
        LIMIT 1
        """,
        (SIGENERGY_PROVIDER, external_id),
    ).fetchone()
    return dict(row) if row is not None else None


def list_enabled_mappings(
    conn: sqlite3.Connection,
    *,
    target_external_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [SIGENERGY_PROVIDER]
    target_clause = ""
    if target_external_ids is not None:
        targets = list(
            dict.fromkeys(str(item).strip() for item in target_external_ids)
        )
        if not targets:
            return []
        target_clause = (
            "AND ai.external_id IN ("
            + ", ".join("?" for _ in targets)
            + ")"
        )
        params.extend(targets)
    rows = conn.execute(
        f"""
        SELECT
            ai.*,
            asset.project_name,
            asset.monitoring_status AS asset_monitoring_status
        FROM asset_integrations ai
        JOIN assets asset ON asset.id = ai.asset_id
        WHERE ai.provider = ?
          AND ai.enabled = 1
          AND COALESCE(ai.external_id, '') != ''
          AND COALESCE(asset.monitoring_status, 'active') != 'disabled'
          {target_clause}
        ORDER BY ai.id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def mapping_status(mapping: dict[str, Any] | None) -> MappingStatus:
    if mapping is None:
        return MappingStatus.UNASSOCIATED
    if not bool(mapping.get("enabled")):
        return MappingStatus.DISABLED
    return MappingStatus.ASSOCIATED


def history_day_is_complete(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    external_id: str,
    target_date: str,
) -> bool:
    row = conn.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM energy_interval_facts
                WHERE asset_id = ?
                  AND provider = ?
                  AND external_id = ?
                  AND granularity = 'day'
                  AND substr(period_start, 1, 10) = ?
                  AND data_quality = 'complete'
            ) AS fact_complete,
            EXISTS (
                SELECT 1
                FROM production_records
                WHERE asset_id = ?
                  AND provider = ?
                  AND external_id = ?
                  AND period_type = 'day'
                  AND period_date = ?
                  AND data_quality = 'complete'
            ) AS production_complete
        """,
        (
            asset_id,
            SIGENERGY_PROVIDER,
            external_id,
            target_date,
            asset_id,
            SIGENERGY_PROVIDER,
            external_id,
            target_date,
        ),
    ).fetchone()
    return bool(row["fact_complete"] and row["production_complete"])


def upsert_accessible_system(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    external_name: str,
    energy_flow: dict[str, Any],
    operational_status: OperationalStatus,
    observed_at: str,
) -> None:
    existing = inventory_identity(conn, external_id)
    metadata: dict[str, Any] = {}
    if existing is not None:
        try:
            parsed = json.loads(existing.get("metadata_json") or "{}")
            if isinstance(parsed, dict):
                metadata = parsed
        except json.JSONDecodeError:
            metadata = {}
    metadata.update(
        {
            "systemId": external_id,
            "systemName": external_name or external_id,
            "validation_method": "direct_energy_flow",
            "energy_flow_fields": sorted(str(key) for key in energy_flow),
        }
    )
    conn.execute(
        """
        INSERT INTO provider_system_inventory (
            provider, external_id, external_name, metadata_json,
            access_status, validation_method, first_discovered_at,
            last_discovered_at, last_state, last_state_at,
            operational_status, sync_status, data_quality, last_error,
            first_access_at, last_access_at, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, 'accessible', 'direct_energy_flow', ?, ?,
            ?, ?, ?, 'never_synced', 'missing', '', ?, ?, ?, ?
        )
        ON CONFLICT(provider, external_id) DO UPDATE SET
            external_name = CASE
                WHEN excluded.external_name != excluded.external_id
                    THEN excluded.external_name
                ELSE provider_system_inventory.external_name
            END,
            metadata_json = excluded.metadata_json,
            access_status = 'accessible',
            validation_method = 'direct_energy_flow',
            last_state = CASE
                WHEN excluded.last_state NOT IN ('', 'Sem dados')
                    THEN excluded.last_state
                ELSE provider_system_inventory.last_state
            END,
            last_state_at = excluded.last_state_at,
            operational_status = excluded.operational_status,
            first_access_at = COALESCE(
                provider_system_inventory.first_access_at,
                excluded.first_access_at
            ),
            last_access_at = excluded.last_access_at,
            updated_at = excluded.updated_at
        """,
        (
            SIGENERGY_PROVIDER,
            external_id,
            external_name or external_id,
            _json(metadata),
            observed_at,
            observed_at,
            _legacy_status_label(operational_status),
            observed_at,
            operational_status.value,
            observed_at,
            observed_at,
            observed_at,
            observed_at,
        ),
    )


def ensure_mapped_inventory(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    external_name: str,
    observed_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO provider_system_inventory (
            provider, external_id, external_name, metadata_json,
            access_status, validation_method, first_discovered_at,
            last_discovered_at, last_state, operational_status, sync_status,
            data_quality, last_error, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, 'unknown', 'mapping', ?, ?, 'Sem dados', 'unknown',
            'never_synced', 'missing', '', ?, ?
        )
        ON CONFLICT(provider, external_id) DO UPDATE SET
            external_name = CASE
                WHEN provider_system_inventory.external_name IN (
                    '',
                    provider_system_inventory.external_id
                )
                THEN excluded.external_name
                ELSE provider_system_inventory.external_name
            END,
            updated_at = excluded.updated_at
        """,
        (
            SIGENERGY_PROVIDER,
            external_id,
            external_name or external_id,
            _json(
                {
                    "systemId": external_id,
                    "systemName": external_name or external_id,
                    "inventory_source": "active_mapping",
                }
            ),
            observed_at,
            observed_at,
            observed_at,
            observed_at,
        ),
    )


def update_access_failure_if_known(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    access_status: AccessStatus,
    observed_at: str,
) -> None:
    if access_status not in {AccessStatus.UNAUTHORIZED, AccessStatus.NOT_FOUND}:
        return
    conn.execute(
        """
        UPDATE provider_system_inventory
        SET access_status = ?, updated_at = ?
        WHERE provider = ? AND external_id = ?
        """,
        (
            access_status.value,
            observed_at,
            SIGENERGY_PROVIDER,
            external_id,
        ),
    )


def insert_realtime_snapshot(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    row: dict[str, Any],
    collected_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO integration_realtime_snapshots (
            asset_id, provider, external_id, collected_at, external_status,
            normalized_status, pv_power_kw, load_power_kw,
            grid_power_kw_raw, battery_power_kw, battery_soc_pct, ev_power_kw,
            ac_power_kw, heat_pump_power_kw, pv_capacity_kw,
            battery_capacity_kwh, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            SIGENERGY_PROVIDER,
            row["external_id"],
            collected_at,
            row.get("raw_status"),
            row.get("status"),
            row.get("pv_power_kw"),
            row.get("load_power_kw"),
            row.get("grid_power_kw_raw"),
            row.get("battery_power_kw"),
            row.get("battery_soc_pct"),
            row.get("ev_power_kw"),
            row.get("ac_power_kw"),
            row.get("heat_pump_power_kw"),
            row.get("pv_capacity_kw"),
            row.get("battery_capacity_kwh"),
            _json(
                {
                    **(row.get("payload") or {}),
                    "fetch_status": "ok",
                    "fetch_error": "",
                }
            ),
        ),
    )
    return int(cursor.lastrowid)


def update_sync_success(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    external_name: str,
    operational_status: OperationalStatus,
    legacy_status: str,
    collected_at: str,
) -> None:
    conn.execute(
        """
        UPDATE asset_integrations
        SET external_name = ?, last_sync_at = ?, last_status = ?,
            last_error = ''
        WHERE provider = ? AND external_id = ? AND enabled = 1
        """,
        (
            external_name or external_id,
            collected_at,
            legacy_status,
            SIGENERGY_PROVIDER,
            external_id,
        ),
    )
    conn.execute(
        """
        UPDATE provider_system_inventory
        SET access_status = 'accessible',
            operational_status = ?,
            sync_status = 'success',
            last_state = ?,
            last_state_at = ?,
            last_telemetry_at = ?,
            last_sync_at = ?,
            last_snapshot_at = ?,
            updated_at = ?
        WHERE provider = ? AND external_id = ?
        """,
        (
            operational_status.value,
            legacy_status,
            collected_at,
            collected_at,
            collected_at,
            collected_at,
            collected_at,
            SIGENERGY_PROVIDER,
            external_id,
        ),
    )


def update_sync_failure(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    sync_status: SyncStatus,
    message: str,
    attempted_at: str,
    access_status: AccessStatus,
) -> None:
    safe_message = sanitize_sigenergy_error(message)[:2000]
    conn.execute(
        """
        UPDATE asset_integrations
        SET last_sync_at = ?, last_error = ?
        WHERE provider = ? AND external_id = ? AND enabled = 1
        """,
        (
            attempted_at,
            safe_message,
            SIGENERGY_PROVIDER,
            external_id,
        ),
    )
    access_update = (
        access_status.value
        if access_status in {AccessStatus.UNAUTHORIZED, AccessStatus.NOT_FOUND}
        else None
    )
    conn.execute(
        """
        UPDATE provider_system_inventory
        SET sync_status = ?,
            access_status = COALESCE(?, access_status),
            last_sync_at = ?,
            updated_at = ?
        WHERE provider = ? AND external_id = ?
        """,
        (
            sync_status.value,
            access_update,
            attempted_at,
            attempted_at,
            SIGENERGY_PROVIDER,
            external_id,
        ),
    )


def set_inventory_data_quality(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    data_quality: DataQuality,
    observed_at: str,
) -> None:
    conn.execute(
        """
        UPDATE provider_system_inventory
        SET last_telemetry_at = ?, data_quality = ?, updated_at = ?
        WHERE provider = ? AND external_id = ?
        """,
        (
            observed_at,
            data_quality.value,
            observed_at,
            SIGENERGY_PROVIDER,
            external_id,
        ),
    )


def _legacy_status_label(status: OperationalStatus) -> str:
    return {
        OperationalStatus.OPERATIONAL: "Operacional",
        OperationalStatus.WARNING: "Aviso",
        OperationalStatus.ERROR: "Erro",
        OperationalStatus.OFFLINE: "Desconectada",
        OperationalStatus.UNKNOWN: "Sem dados",
    }[status]
