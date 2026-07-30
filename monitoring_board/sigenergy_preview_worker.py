from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from monitoring_board.db import get_db
from monitoring_board.runtime import DB_PATH
from monitoring_board.services.api_rate_limit import ApiRateLimitError
from monitoring_board.services.energy_facts import (
    parse_sigenergy_daily_history,
    persist_sigenergy_daily_history,
)
from monitoring_board.services.sigenergy_client import (
    EXPERTCOM_SIGENERGY_BASE_URL,
    EXPERTCOM_SIGENERGY_SYSTEM_ID,
    SigenergyClient,
    SigenergyPreviewReadOnlyPolicy,
)
from monitoring_board.services.sigenergy_models import (
    SigenergyCredentials,
    SigenergyEndpoints,
    map_sigenergy_status,
    normalize_energy_flow,
    normalize_system,
    sanitize_payload,
    sanitize_sigenergy_error,
)


LISBON = ZoneInfo("Europe/Lisbon")
PROVIDER = "Sigenergy"
EXPERTCOM_NAME = "Expertcom"
DEFAULT_HISTORY_INTERVAL_SECONDS = 300.0
REQUIRED_FALSE_FLAGS = (
    "EXTERNAL_ACTIONS_ENABLED",
    "SCHEDULER_ENABLED",
    "FUSIONSOLAR_PRODUCTION_SYNC_ENABLED",
    "FUSIONSOLAR_DIAGNOSTICS_SYNC_ENABLED",
    "TELEGRAM_ALERTS_ENABLED",
    "TELEGRAM_DAILY_SUMMARY_ENABLED",
)


def _flag_is_false(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "0",
        "false",
        "no",
        "off",
        "nao",
        "não",
    }


def validate_worker_environment() -> None:
    if os.environ.get("APP_ENV", "").strip().casefold() != "preview":
        raise ValueError("O worker Sigenergy read-only so pode correr em preview.")
    if os.environ.get("PREVIEW_BANNER", "").strip().casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise ValueError("PREVIEW_BANNER=true e obrigatorio no worker Sigenergy.")
    for name in REQUIRED_FALSE_FLAGS:
        if not _flag_is_false(name):
            raise ValueError(f"{name}=false e obrigatorio no worker Sigenergy.")

    base_url = os.environ.get("SIGENERGY_BASE_URL", "").strip().rstrip("/")
    if base_url != EXPERTCOM_SIGENERGY_BASE_URL:
        raise ValueError("A Base URL Sigenergy nao pertence a allowlist da preview.")
    if os.environ.get("SIGENERGY_REGION", "").strip() != "eu":
        raise ValueError("A regiao Sigenergy do worker tem de ser eu.")
    allowed_ids = [
        item.strip()
        for item in os.environ.get(
            "SIGENERGY_ALLOWED_SYSTEM_IDS",
            "",
        ).split(",")
        if item.strip()
    ]
    if allowed_ids != [EXPERTCOM_SIGENERGY_SYSTEM_ID]:
        raise ValueError(
            "A allowlist do worker tem de conter exclusivamente a Expertcom."
        )
    if os.environ.get("SIGENERGY_HISTORY_ENERGY_UNIT", "").strip().casefold() != "kwh":
        raise ValueError("A unidade historica Sigenergy tem de ser kWh.")
    if not os.environ.get("SIGENERGY_APP_KEY", "").strip() or not os.environ.get(
        "SIGENERGY_APP_SECRET",
        "",
    ).strip():
        raise ValueError("Configura App Key e App Secret no ambiente root-only.")


def build_worker_client(
    *,
    session: requests.Session | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> SigenergyClient:
    validate_worker_environment()
    endpoints = SigenergyEndpoints(
        base_url=EXPERTCOM_SIGENERGY_BASE_URL,
        login_endpoint="/openapi/auth/login/key",
        systems_endpoint="/openapi/system",
        energy_flow_endpoint=(
            f"/openapi/systems/{EXPERTCOM_SIGENERGY_SYSTEM_ID}/energyFlow"
        ),
        history_endpoint=(
            f"/openapi/systems/{EXPERTCOM_SIGENERGY_SYSTEM_ID}/history"
        ),
        region="eu",
    )
    credentials = SigenergyCredentials(
        os.environ["SIGENERGY_APP_KEY"].strip(),
        os.environ["SIGENERGY_APP_SECRET"].strip(),
    )
    return SigenergyClient(
        endpoints,
        credentials,
        session=session,
        allow_sleep=True,
        sleeper=sleeper,
        read_only_policy=SigenergyPreviewReadOnlyPolicy(),
    )


def _inventory_system(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM provider_system_inventory
        WHERE provider = ? AND external_id = ? AND access_status = 'accessible'
        """,
        (PROVIDER, EXPERTCOM_SIGENERGY_SYSTEM_ID),
    ).fetchone()
    if row is None:
        raise ValueError(
            "A Expertcom ainda nao foi descoberta no inventario da preview."
        )
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def discover(
    conn: sqlite3.Connection,
    client: SigenergyClient,
) -> dict[str, Any]:
    systems = client.list_systems(allow_empty=True)
    selected = [
        system
        for system in systems
        if normalize_system(system)["external_id"]
        == EXPERTCOM_SIGENERGY_SYSTEM_ID
    ]
    if len(selected) > 1:
        raise ValueError("A descoberta devolveu a Expertcom mais de uma vez.")
    validation_method = "discovery"
    discovery_returned = bool(selected)
    if selected:
        system = selected[0]
    else:
        client.get_energy_flow(EXPERTCOM_SIGENERGY_SYSTEM_ID)
        system = {
            "systemId": EXPERTCOM_SIGENERGY_SYSTEM_ID,
            "systemName": EXPERTCOM_NAME,
            "validation_method": "direct_energy_flow",
            "discovery_returned": False,
        }
        validation_method = "direct_energy_flow"
    normalized = normalize_system(system)
    now = datetime.now(LISBON).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO provider_system_inventory (
            provider, external_id, external_name, metadata_json,
            access_status, validation_method, first_discovered_at, last_discovered_at,
            last_state, data_quality, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'accessible', ?, ?, ?, ?, 'missing', '', ?, ?)
        ON CONFLICT(provider, external_id) DO UPDATE SET
            external_name = excluded.external_name,
            metadata_json = excluded.metadata_json,
            access_status = 'accessible',
            validation_method = excluded.validation_method,
            last_discovered_at = excluded.last_discovered_at,
            last_state = excluded.last_state,
            last_error = '',
            updated_at = excluded.updated_at
        """,
        (
            PROVIDER,
            EXPERTCOM_SIGENERGY_SYSTEM_ID,
            normalized["external_name"],
            json.dumps(sanitize_payload(system), ensure_ascii=True),
            validation_method,
            now,
            now,
            normalized["normalized_status"],
            now,
            now,
        ),
    )
    if validation_method == "direct_energy_flow":
        conn.execute(
            """
            INSERT INTO sigenergy_access_validations (
                system_id, validation_method, discovery_returned, outcome,
                status_code, sanitized_error, details_json, created_at
            ) VALUES (?, 'direct_energy_flow', 0, 'available', NULL, '', ?, ?)
            """,
            (
                EXPERTCOM_SIGENERGY_SYSTEM_ID,
                json.dumps(
                    {
                        "source": "preview_read_only_worker",
                        "discovery_returned": False,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                now,
            ),
        )
    conn.commit()
    return {
        "name": normalized["external_name"],
        "system_id": EXPERTCOM_SIGENERGY_SYSTEM_ID,
        "status": "discovered",
        "validation_method": validation_method,
        "discovery_returned": discovery_returned,
        "assets_created": 0,
    }


def update_state(
    conn: sqlite3.Connection,
    client: SigenergyClient,
) -> dict[str, Any]:
    system = _inventory_system(conn)
    flow = client.get_energy_flow(EXPERTCOM_SIGENERGY_SYSTEM_ID)
    normalized_system = normalize_system(system)
    normalized_flow = normalize_energy_flow(flow)
    status = map_sigenergy_status(
        system.get("status")
        or system.get("systemStatus")
        or system.get("runningStatus")
        or system.get("state")
    )
    mapping = conn.execute(
        """
        SELECT asset_id
        FROM asset_integrations
        WHERE provider = ? AND external_id = ? AND enabled = 1
        """,
        (PROVIDER, EXPERTCOM_SIGENERGY_SYSTEM_ID),
    ).fetchone()
    asset_id = int(mapping["asset_id"]) if mapping is not None else None
    collected_at = datetime.now(LISBON).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )
    conn.execute(
        """
        INSERT INTO integration_realtime_snapshots (
            asset_id, provider, external_id, collected_at, external_status,
            normalized_status, pv_power_kw, load_power_kw, grid_power_kw_raw,
            battery_power_kw, battery_soc_pct, ev_power_kw, ac_power_kw,
            heat_pump_power_kw, pv_capacity_kw, battery_capacity_kwh,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            PROVIDER,
            EXPERTCOM_SIGENERGY_SYSTEM_ID,
            collected_at,
            normalized_system["raw_status"],
            status,
            normalized_flow["pv_power_kw"],
            normalized_flow["load_power_kw"],
            normalized_flow["grid_power_kw_raw"],
            normalized_flow["battery_power_kw"],
            normalized_flow["battery_soc_pct"],
            normalized_flow["ev_power_kw"],
            normalized_flow["ac_power_kw"],
            normalized_flow["heat_pump_power_kw"],
            normalized_system["pv_capacity_kw"],
            normalized_system["battery_capacity_kwh"],
            json.dumps(
                sanitize_payload({"system": system, "energy_flow": flow}),
                ensure_ascii=True,
            ),
        ),
    )
    conn.execute(
        """
        UPDATE provider_system_inventory
        SET last_state = ?, last_state_at = ?, last_telemetry_at = ?,
            last_error = '', updated_at = ?
        WHERE provider = ? AND external_id = ?
        """,
        (
            status,
            collected_at,
            collected_at,
            collected_at,
            PROVIDER,
            EXPERTCOM_SIGENERGY_SYSTEM_ID,
        ),
    )
    conn.commit()
    return {
        "name": normalized_system["external_name"],
        "system_id": EXPERTCOM_SIGENERGY_SYSTEM_ID,
        "status": status,
        "snapshot_at": collected_at,
        "alerts_sent": 0,
    }


def _validate_backfill_dates(date_from: date, date_to: date) -> int:
    if date_from > date_to:
        raise ValueError("A data inicial nao pode ser posterior a data final.")
    if date_to >= datetime.now(LISBON).date():
        raise ValueError("O backfill aceita exclusivamente dias terminados.")
    day_count = (date_to - date_from).days + 1
    if day_count > 31:
        raise ValueError("O backfill da preview esta limitado a 31 dias.")
    return day_count


def backfill(
    conn: sqlite3.Connection,
    client: SigenergyClient,
    *,
    date_from: date,
    date_to: date,
    minimum_interval_seconds: float | None = None,
) -> dict[str, Any]:
    day_count = _validate_backfill_dates(date_from, date_to)
    if minimum_interval_seconds is None:
        raw_interval = os.environ.get(
            "SIGENERGY_PRODUCTION_MIN_INTERVAL_SECONDS",
            str(int(DEFAULT_HISTORY_INTERVAL_SECONDS)),
        ).strip()
        try:
            minimum_interval_seconds = float(raw_interval)
        except ValueError as exc:
            raise ValueError(
                "O intervalo de histórico Sigenergy tem de ser numérico."
            ) from exc
        if minimum_interval_seconds < DEFAULT_HISTORY_INTERVAL_SECONDS:
            raise ValueError(
                "O worker exige pelo menos 300 segundos entre dias históricos."
            )
    _inventory_system(conn)
    mapping = conn.execute(
        """
        SELECT asset_id
        FROM asset_integrations
        WHERE provider = ? AND external_id = ? AND enabled = 1
        """,
        (PROVIDER, EXPERTCOM_SIGENERGY_SYSTEM_ID),
    ).fetchone()
    if mapping is None:
        raise ValueError("Associa primeiro a Expertcom a um asset local.")
    asset_id = int(mapping["asset_id"])
    completed: list[str] = []
    partial: list[str] = []
    failed: list[dict[str, str]] = []

    for offset in range(day_count):
        target_date = date_from + timedelta(days=offset)
        try:
            history = client.get_system_history(
                EXPERTCOM_SIGENERGY_SYSTEM_ID,
                level="Day",
                target_date=target_date.isoformat(),
            )
            fact = parse_sigenergy_daily_history(
                history,
                system_id=EXPERTCOM_SIGENERGY_SYSTEM_ID,
                period_date=target_date,
                confirmed_unit="kWh",
            )
            persist_sigenergy_daily_history(
                conn,
                asset_id=asset_id,
                fact=fact,
            )
            now = datetime.now(LISBON).replace(tzinfo=None).isoformat(
                timespec="seconds"
            )
            conn.execute(
                """
                UPDATE provider_system_inventory
                SET last_telemetry_at = ?, data_quality = ?, last_error = '',
                    updated_at = ?
                WHERE provider = ? AND external_id = ?
                """,
                (
                    now,
                    fact.data_quality,
                    now,
                    PROVIDER,
                    EXPERTCOM_SIGENERGY_SYSTEM_ID,
                ),
            )
            conn.commit()
            if fact.data_quality == "complete":
                completed.append(target_date.isoformat())
            else:
                partial.append(target_date.isoformat())
        except Exception as exc:
            conn.rollback()
            failed.append(
                {
                    "date": target_date.isoformat(),
                    "error": sanitize_sigenergy_error(exc),
                }
            )
            if isinstance(exc, ApiRateLimitError):
                for remaining_offset in range(offset + 1, day_count):
                    failed.append(
                        {
                            "date": (
                                date_from + timedelta(days=remaining_offset)
                            ).isoformat(),
                            "error": "nao tentado devido ao rate limit",
                        }
                    )
                break
        if offset + 1 < day_count and minimum_interval_seconds > 0:
            time.sleep(minimum_interval_seconds)

    month_rows = conn.execute(
        """
        SELECT period_date, data_quality, production_kwh, consumption_kwh,
               self_use_kwh, export_kwh, grid_import_kwh
        FROM production_records
        WHERE asset_id = ? AND provider = ? AND period_type = 'month'
          AND period_date BETWEEN ? AND ?
        ORDER BY period_date
        """,
        (
            asset_id,
            PROVIDER,
            date_from.replace(day=1).isoformat(),
            date_to.replace(day=1).isoformat(),
        ),
    ).fetchall()
    return {
        "system_id": EXPERTCOM_SIGENERGY_SYSTEM_ID,
        "completed": completed,
        "partial": partial,
        "failed": failed,
        "months": [dict(row) for row in month_rows],
    }


def check(client: SigenergyClient) -> dict[str, Any]:
    systems = client.list_systems(allow_empty=True)
    discovery_returned = any(
        normalize_system(system)["external_id"]
        == EXPERTCOM_SIGENERGY_SYSTEM_ID
        for system in systems
    )
    client.get_energy_flow(EXPERTCOM_SIGENERGY_SYSTEM_ID)
    return {
        "name": EXPERTCOM_NAME,
        "system_id": EXPERTCOM_SIGENERGY_SYSTEM_ID,
        "base_url": EXPERTCOM_SIGENERGY_BASE_URL,
        "status": "read_only_access_ok",
        "validation_method": (
            "discovery" if discovery_returned else "direct_energy_flow"
        ),
        "discovery_returned": discovery_returned,
    }


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Usa datas no formato AAAA-MM-DD.") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Worker one-shot Sigenergy read-only da preview."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover")
    subparsers.add_parser("state")
    backfill_parser = subparsers.add_parser("backfill")
    backfill_parser.add_argument("date_from", type=_parse_iso_date)
    backfill_parser.add_argument("date_to", type=_parse_iso_date)
    subparsers.add_parser("check")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = build_worker_client()
    if args.command == "check":
        result = check(client)
    else:
        with get_db(str(DB_PATH)) as conn:
            if args.command == "discover":
                result = discover(conn, client)
            elif args.command == "state":
                result = update_state(conn, client)
            else:
                result = backfill(
                    conn,
                    client,
                    date_from=args.date_from,
                    date_to=args.date_to,
                )
    print(json.dumps(sanitize_payload(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
