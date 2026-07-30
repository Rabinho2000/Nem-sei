from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from monitoring_board.services.sigenergy_contracts import (
    validate_sigenergy_system_id,
)


SENSITIVE_KEY = re.compile(
    r"(secret|password|token|authorization|credential|app.?key|username)",
    re.IGNORECASE,
)
IDENTITY_KEYS = {
    "external_id",
    "plantcode",
    "plantid",
    "stationcode",
    "stationid",
    "systemid",
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sha256(value: str | bytes) -> str:
    payload = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        )
    ]


def _matching_tables(
    conn: sqlite3.Connection,
    target: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for table in _table_names(conn):
        searchable = [
            str(column["name"])
            for column in _columns(conn, table)
            if "BLOB" not in str(column.get("type") or "").upper()
        ]
        if not searchable:
            continue
        column_counts: list[dict[str, Any]] = []
        for column in searchable:
            count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {_quote_identifier(table)}
                    WHERE instr(
                        CAST({_quote_identifier(column)} AS TEXT),
                        ?
                    ) > 0
                    """,
                    (target,),
                ).fetchone()[0]
            )
            if count:
                column_counts.append({"column": column, "rows": count})
        if not column_counts:
            continue
        predicate = " OR ".join(
            (
                "instr(CAST("
                + _quote_identifier(item["column"])
                + " AS TEXT), ?) > 0"
            )
            for item in column_counts
        )
        matched_rows = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {_quote_identifier(table)}
                WHERE {predicate}
                """,
                tuple(target for _item in column_counts),
            ).fetchone()[0]
        )
        matches.append(
            {
                "table": table,
                "matched_rows": matched_rows,
                "matched_columns": column_counts,
            }
        )
    return matches


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table,),
        ).fetchone()
        is not None
    )


def _has_columns(
    conn: sqlite3.Connection,
    table: str,
    required: Iterable[str],
) -> bool:
    if not _has_table(conn, table):
        return False
    available = {str(row["name"]) for row in _columns(conn, table)}
    return set(required) <= available


def _json_shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        if isinstance(value, dict):
            return {"type": "object", "keys": sorted(str(key) for key in value)}
        if isinstance(value, list):
            return {"type": "array", "length": len(value)}
        return type(value).__name__
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if SENSITIVE_KEY.search(str(key))
                else _json_shape(item, depth=depth + 1)
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "item_shapes": [
                _json_shape(item, depth=depth + 1) for item in value[:3]
            ],
        }
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _find_identity_values(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized_key in IDENTITY_KEYS and isinstance(
                item,
                (str, int),
            ):
                found.append(str(item))
            found.extend(_find_identity_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_identity_values(item))
    return list(dict.fromkeys(found))


def _synthetic_identity_signature(value: Any, target: str) -> bool:
    if not isinstance(value, dict):
        return False
    candidates: list[dict[str, Any]] = [value]
    candidates.extend(
        item for item in value.values() if isinstance(item, dict)
    )
    for candidate in candidates:
        system_id = str(
            candidate.get("systemId")
            or candidate.get("external_id")
            or ""
        ).strip()
        system_name = str(
            candidate.get("systemName")
            or candidate.get("external_name")
            or ""
        ).strip()
        if system_id == target and system_name == target:
            return True
    return False


def _payload_summary(raw_payload: Any, target: str) -> dict[str, Any]:
    raw = str(raw_payload or "")
    try:
        parsed = json.loads(raw)
        valid_json = True
    except (TypeError, json.JSONDecodeError):
        parsed = None
        valid_json = False
    identity_values = _find_identity_values(parsed) if valid_json else []
    return {
        "bytes": len(raw.encode("utf-8", errors="replace")),
        "sha256": _sha256(raw),
        "valid_json": valid_json,
        "shape": _json_shape(parsed) if valid_json else "non_json_text",
        "target_present": target in raw,
        "identity_values": [
            value for value in identity_values if value == target
        ],
        "synthetic_identity_signature": (
            _synthetic_identity_signature(parsed, target)
            if valid_json
            else False
        ),
    }


def _snapshot_evidence(
    conn: sqlite3.Connection,
    target: str,
) -> dict[str, Any]:
    table = "integration_realtime_snapshots"
    required = {
        "id",
        "asset_id",
        "provider",
        "external_id",
        "collected_at",
        "payload_json",
    }
    if not _has_columns(conn, table, required):
        return {"available": False}
    summary = conn.execute(
        """
        SELECT
            COUNT(*) AS snapshot_count,
            MIN(collected_at) AS first_collected_at,
            MAX(collected_at) AS last_collected_at,
            MIN(id) AS first_id,
            MAX(id) AS last_id
        FROM integration_realtime_snapshots
        WHERE provider = 'Sigenergy' AND external_id = ?
        """,
        (target,),
    ).fetchone()
    asset_ids = [
        row["asset_id"]
        for row in conn.execute(
            """
            SELECT DISTINCT asset_id
            FROM integration_realtime_snapshots
            WHERE provider = 'Sigenergy' AND external_id = ?
            ORDER BY asset_id
            """,
            (target,),
        )
    ]
    boundary_rows = conn.execute(
        """
        SELECT id, asset_id, collected_at, payload_json
        FROM integration_realtime_snapshots
        WHERE provider = 'Sigenergy' AND external_id = ?
        ORDER BY collected_at, id
        """,
        (target,),
    ).fetchall()
    boundaries: dict[str, Any] = {}
    if boundary_rows:
        first = boundary_rows[0]
        last = boundary_rows[-1]
        boundaries = {
            "first": {
                "id": int(first["id"]),
                "asset_id": first["asset_id"],
                "collected_at": first["collected_at"],
                "payload": _payload_summary(first["payload_json"], target),
            },
            "last": {
                "id": int(last["id"]),
                "asset_id": last["asset_id"],
                "collected_at": last["collected_at"],
                "payload": _payload_summary(last["payload_json"], target),
            },
        }
    return {
        "available": True,
        "snapshot_count": int(summary["snapshot_count"] or 0),
        "first_collected_at": summary["first_collected_at"],
        "last_collected_at": summary["last_collected_at"],
        "first_id": summary["first_id"],
        "last_id": summary["last_id"],
        "asset_ids": asset_ids,
        "boundaries": boundaries,
    }


def _mapping_evidence(
    conn: sqlite3.Connection,
    target: str,
) -> list[dict[str, Any]]:
    table = "asset_integrations"
    required = {"id", "asset_id", "provider", "external_id", "enabled"}
    if not _has_columns(conn, table, required):
        return []
    available = {str(row["name"]) for row in _columns(conn, table)}
    optional = [
        name
        for name in (
            "is_primary_energy_source",
            "last_sync_at",
            "created_at",
            "updated_at",
        )
        if name in available
    ]
    selected = ["id", "asset_id", "enabled", *optional]
    rows = conn.execute(
        f"""
        SELECT {", ".join(_quote_identifier(name) for name in selected)}
        FROM asset_integrations
        WHERE provider = 'Sigenergy' AND external_id = ?
        ORDER BY id
        """,
        (target,),
    ).fetchall()
    return [dict(row) for row in rows]


def _config_evidence(
    conn: sqlite3.Connection,
    target: str,
) -> dict[str, Any]:
    table = "integration_configs"
    if not _has_columns(conn, table, {"provider"}):
        return {"available": False}
    available = {str(row["name"]) for row in _columns(conn, table)}
    safe_columns = [
        name
        for name in (
            "enabled",
            "auto_sync_enabled",
            "last_sync_at",
            "last_sync_status",
            "created_at",
            "updated_at",
        )
        if name in available
    ]
    selected = [*safe_columns]
    if "system_ids" in available:
        selected.append("system_ids")
    if "last_error" in available:
        selected.append("last_error")
    if not selected:
        return {"available": True}
    row = conn.execute(
        f"""
        SELECT {", ".join(_quote_identifier(name) for name in selected)}
        FROM integration_configs
        WHERE provider = 'Sigenergy'
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {"available": True, "configured": False}
    result = {
        "available": True,
        "configured": True,
        **{name: row[name] for name in safe_columns},
    }
    raw_system_ids = str(row["system_ids"] or "") if "system_ids" in selected else ""
    tokens = [
        item
        for item in re.split(r"[,;\s]+", raw_system_ids)
        if item
    ]
    result.update(
        {
            "legacy_system_ids_present": bool(raw_system_ids.strip()),
            "target_in_legacy_system_ids": target in tokens,
            "legacy_system_ids_sha256": (
                _sha256(raw_system_ids) if raw_system_ids else ""
            ),
            "legacy_system_id_count": len(tokens),
            "target_in_legacy_last_error": (
                target in str(row["last_error"] or "")
                if "last_error" in selected
                else False
            ),
        }
    )
    return result


def _run_evidence(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    table = "integration_sync_runs"
    required = {"id", "provider", "status", "started_at"}
    if not _has_columns(conn, table, required):
        return []
    available = {str(row["name"]) for row in _columns(conn, table)}
    selected = [
        name
        for name in (
            "id",
            "trigger_type",
            "status",
            "started_at",
            "finished_at",
            "matched_count",
            "unresolved_count",
            "auto_resolved_count",
        )
        if name in available
    ]
    first_at = snapshot.get("first_collected_at")
    last_at = snapshot.get("last_collected_at")
    if first_at and last_at:
        rows = conn.execute(
            f"""
            SELECT {", ".join(_quote_identifier(name) for name in selected)}
            FROM integration_sync_runs
            WHERE provider = 'Sigenergy'
              AND started_at <= datetime(?, '+10 minutes')
              AND COALESCE(finished_at, started_at) >= datetime(?, '-10 minutes')
            ORDER BY started_at, id
            """,
            (last_at, first_at),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {", ".join(_quote_identifier(name) for name in selected)}
            FROM integration_sync_runs
            WHERE provider = 'Sigenergy'
            ORDER BY started_at, id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _background_job_evidence(
    conn: sqlite3.Connection,
    target: str,
) -> list[dict[str, Any]]:
    table = "background_jobs"
    required = {"id", "job_type", "status", "params_json", "created_at"}
    if not _has_columns(conn, table, required):
        return []
    available = {str(row["name"]) for row in _columns(conn, table)}
    selected = [
        name
        for name in (
            "id",
            "job_type",
            "status",
            "params_json",
            "created_at",
            "started_at",
            "finished_at",
        )
        if name in available
    ]
    rows = conn.execute(
        f"""
        SELECT {", ".join(_quote_identifier(name) for name in selected)}
        FROM background_jobs
        WHERE instr(CAST(params_json AS TEXT), ?) > 0
        ORDER BY id
        """,
        (target,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        raw_params = str(row["params_json"] or "")
        try:
            params = json.loads(raw_params)
        except json.JSONDecodeError:
            params = None
        safe = {
            name: row[name]
            for name in selected
            if name != "params_json"
        }
        safe.update(
            {
                "params_sha256": _sha256(raw_params),
                "params_keys": (
                    sorted(str(key) for key in params)
                    if isinstance(params, dict)
                    else []
                ),
                "target_present": target in raw_params,
                "target_date": (
                    str(params.get("target_date") or "")
                    if isinstance(params, dict)
                    else ""
                ),
            }
        )
        results.append(safe)
    return results


def _fact_evidence(
    conn: sqlite3.Connection,
    table: str,
    target: str,
) -> dict[str, Any]:
    required = {"provider", "external_id"}
    if not _has_columns(conn, table, required):
        return {"available": False}
    available = {str(row["name"]) for row in _columns(conn, table)}
    date_column = (
        "period_start"
        if "period_start" in available
        else "period_date"
        if "period_date" in available
        else None
    )
    quality_column = "data_quality" if "data_quality" in available else None
    asset_column = "asset_id" if "asset_id" in available else None
    select_parts = ["COUNT(*) AS row_count"]
    if date_column:
        select_parts.extend(
            [
                f"MIN({_quote_identifier(date_column)}) AS first_period",
                f"MAX({_quote_identifier(date_column)}) AS last_period",
            ]
        )
    if quality_column:
        select_parts.append(
            (
                "SUM(CASE WHEN "
                + _quote_identifier(quality_column)
                + " = 'complete' THEN 1 ELSE 0 END) AS complete_count"
            )
        )
    row = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM {_quote_identifier(table)}
        WHERE provider = 'Sigenergy' AND external_id = ?
        """,
        (target,),
    ).fetchone()
    result = {"available": True, **dict(row)}
    if asset_column:
        result["asset_ids"] = [
            item["asset_id"]
            for item in conn.execute(
                f"""
                SELECT DISTINCT asset_id
                FROM {_quote_identifier(table)}
                WHERE provider = 'Sigenergy' AND external_id = ?
                ORDER BY asset_id
                """,
                (target,),
            )
        ]
    return result


def _classify_origin(
    *,
    snapshot: dict[str, Any],
    mappings: list[dict[str, Any]],
    config: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    boundaries = snapshot.get("boundaries") or {}
    synthetic_signature = any(
        bool((boundaries.get(edge) or {}).get("payload", {}).get(
            "synthetic_identity_signature"
        ))
        for edge in ("first", "last")
    )
    evidence = {
        "legacy_system_ids_contains_target": bool(
            config.get("target_in_legacy_system_ids")
        ),
        "synthetic_identity_signature": synthetic_signature,
        "mapping_count": len(mappings),
        "background_job_count": len(jobs),
        "snapshots_without_asset": (
            snapshot.get("asset_ids") == [None]
            if snapshot.get("snapshot_count")
            else False
        ),
    }
    if (
        evidence["legacy_system_ids_contains_target"]
        and synthetic_signature
        and not mappings
    ):
        conclusion = "legacy_system_ids_synthetic_inventory_path"
        confidence = "high"
    elif mappings:
        conclusion = "mapped_sigenergy_sync_path"
        confidence = "medium"
    elif jobs:
        conclusion = "explicit_background_job_path"
        confidence = "medium"
    else:
        conclusion = "undetermined_from_database_evidence"
        confidence = "low"
    return {
        "conclusion": conclusion,
        "confidence": confidence,
        "evidence": evidence,
        "note": (
            "A classificacao e baseada apenas no backup. Deve ser cruzada "
            "com a evidencia Git documentada no repositorio."
        ),
    }


def audit_backup(database: Path, system_id: str) -> dict[str, Any]:
    target = validate_sigenergy_system_id(system_id)
    resolved = database.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("O caminho indicado nao e um ficheiro de backup.")
    if resolved.name == "monitoring_board.db":
        raise ValueError(
            "A auditoria recusa o nome da base live. Usa uma copia de backup "
            "com nome distinto."
        )
    wal_path = Path(f"{resolved}-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise ValueError(
            "O backup tem um WAL nao consolidado. Cria uma copia consistente "
            "com a SQLite Backup API antes da auditoria."
        )
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        snapshot = _snapshot_evidence(conn, target)
        mappings = _mapping_evidence(conn, target)
        config = _config_evidence(conn, target)
        jobs = _background_job_evidence(conn, target)
        result = {
            "audit_version": 1,
            "mode": "sqlite_read_only_immutable",
            "database": {
                "filename": resolved.name,
                "size_bytes": resolved.stat().st_size,
                "sha256": _file_sha256(resolved),
                "quick_check": quick_check,
            },
            "target_system_id": target,
            "matching_tables": _matching_tables(conn, target),
            "snapshots": snapshot,
            "asset_integrations": mappings,
            "integration_config": config,
            "integration_runs": _run_evidence(conn, snapshot),
            "background_jobs": jobs,
            "energy_interval_facts": _fact_evidence(
                conn,
                "energy_interval_facts",
                target,
            ),
            "production_records": _fact_evidence(
                conn,
                "production_records",
                target,
            ),
        }
        result["origin_classification"] = _classify_origin(
            snapshot=snapshot,
            mappings=mappings,
            config=config,
            jobs=jobs,
        )
        return result
    finally:
        conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audita um System ID numa copia SQLite em modo read-only e emite "
            "apenas evidencia sanitizada."
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="Caminho para uma copia de backup (nao a base live).",
    )
    parser.add_argument("--system-id", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="Opcional: guardar o JSON sanitizado neste ficheiro local.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output: Path | None = None
    try:
        database = args.database.expanduser().resolve(strict=True)
        if args.output is not None:
            output = args.output.expanduser().resolve()
            if output == database:
                raise ValueError(
                    "O ficheiro de saida nao pode ser o proprio backup auditado."
                )
            if output.exists():
                raise ValueError(
                    "O ficheiro de saida ja existe; escolhe um caminho novo."
                )
        result = audit_backup(database, args.system_id)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(
        result,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    if output is not None:
        try:
            with output.open("x", encoding="utf-8") as handle:
                handle.write(rendered + "\n")
        except OSError as exc:
            print(f"ERRO: nao foi possivel criar a saida: {exc}", file=sys.stderr)
            return 2
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
