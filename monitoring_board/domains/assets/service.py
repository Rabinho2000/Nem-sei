"""Asset, installation-group and contract-status workflows."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import date, datetime
from typing import Any

from monitoring_board.db import query_all


GROUP_INHERITED_FIELDS = [
    "company_name",
    "location",
    "address",
    "contract_type",
    "contact_name",
    "contact_role",
    "contact_email",
    "contact_phone",
    "access_type",
    "coverage_type",
]


def normalize_name(value: str) -> str:
    lowered = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    cleaned = "".join(char if char.isalnum() else " " for char in lowered)
    return " ".join(cleaned.split())


def infer_installation_group(project_name: str) -> str:
    name = (project_name or "").strip()
    if not name:
        return ""
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    return stripped or name


def parse_date_value(value: str | None) -> date | None:
    if value in (None, "", "-"):
        return None
    raw_value = str(value).strip()
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw_value, date_format).date()
        except ValueError:
            continue
    return None


def normalize_date_value(value: str | None) -> str:
    parsed = parse_date_value(value)
    return parsed.isoformat() if parsed else (str(value).strip() if value else "")


def derive_active_contract(end_date: str | None, current_value: str = "") -> str:
    parsed_end_date = parse_date_value(end_date)
    if parsed_end_date is None:
        return current_value
    return "yes" if parsed_end_date >= date.today() else "no"


def contract_end_sql() -> str:
    return "COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, ''))"


def apply_group_defaults(
    conn: sqlite3.Connection,
    payload: dict[str, str],
    installation_group: str,
    exclude_asset_id: int | None = None,
) -> dict[str, str]:
    if not installation_group:
        return payload
    available_fields = [field for field in GROUP_INHERITED_FIELDS if field in payload]
    if not available_fields:
        return payload

    conditions = ["installation_group = ?"]
    params: list[Any] = [installation_group]
    if exclude_asset_id is not None:
        conditions.append("id != ?")
        params.append(exclude_asset_id)

    sources = conn.execute(
        f"""
        SELECT {", ".join(available_fields)}
        FROM assets
        WHERE {" AND ".join(conditions)}
          AND ({ " OR ".join(f"NULLIF({field}, '') IS NOT NULL" for field in available_fields) })
        ORDER BY id ASC
        """,
        params,
    ).fetchall()

    for source in sources:
        for field in available_fields:
            if not payload.get(field) and source[field]:
                payload[field] = source[field]
        if all(payload.get(field) for field in available_fields):
            break
    return payload


def list_installation_group_options(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT
            COALESCE(NULLIF(TRIM(installation_group), ''), project_name) AS name,
            COUNT(*) AS member_count
        FROM assets
        WHERE COALESCE(NULLIF(TRIM(installation_group), ''), project_name) != ''
        GROUP BY name
        ORDER BY name COLLATE NOCASE
        """,
    )


def apply_group_defaults_to_asset(conn: sqlite3.Connection, asset_id: int, installation_group: str) -> None:
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return
    payload = {field: asset[field] or "" for field in GROUP_INHERITED_FIELDS}
    updated_payload = apply_group_defaults(conn, payload, installation_group, exclude_asset_id=asset_id)
    changed_fields = [field for field in GROUP_INHERITED_FIELDS if (asset[field] or "") != updated_payload.get(field, "")]
    if not changed_fields:
        return
    assignments = ", ".join(f"{field} = ?" for field in changed_fields)
    values = [updated_payload[field] for field in changed_fields]
    conn.execute(f"UPDATE assets SET {assignments} WHERE id = ?", values + [asset_id])


def populate_missing_group_metadata(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, installation_group
        FROM assets
        WHERE installation_group IS NOT NULL AND TRIM(installation_group) != ''
        ORDER BY installation_group COLLATE NOCASE, id
        """
    ).fetchall()
    for row in rows:
        apply_group_defaults_to_asset(conn, row["id"], row["installation_group"])


def sync_asset_contract_status(
    conn: sqlite3.Connection,
    asset_id: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    asset = conn.execute("SELECT active_contract, start_contract, end_contract FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return
    contract = conn.execute(
        "SELECT contract_start_date, contract_end_date FROM om_contracts WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    final_start = normalize_date_value(start_date or (contract["contract_start_date"] if contract else "") or asset["start_contract"])
    final_end = normalize_date_value(end_date or (contract["contract_end_date"] if contract else "") or asset["end_contract"])
    active_contract = derive_active_contract(final_end, asset["active_contract"] or "")
    conn.execute(
        """
        UPDATE assets
        SET maintenance = CASE WHEN ? = 'yes' THEN 'yes' ELSE maintenance END,
            active_contract = ?,
            start_contract = CASE WHEN ? != '' THEN ? ELSE start_contract END,
            end_contract = CASE WHEN ? != '' THEN ? ELSE end_contract END
        WHERE id = ?
        """,
        (active_contract, active_contract, final_start, final_start, final_end, final_end, asset_id),
    )


def sync_all_contract_statuses(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT a.id, a.start_contract, a.end_contract, oc.contract_start_date, oc.contract_end_date
        FROM assets a
        LEFT JOIN om_contracts oc ON oc.asset_id = a.id
        WHERE COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, '')) IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        sync_asset_contract_status(
            conn,
            row["id"],
            row["contract_start_date"] or row["start_contract"],
            row["contract_end_date"] or row["end_contract"],
        )


def populate_missing_installation_groups(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, project_name
        FROM assets
        WHERE installation_group IS NULL OR TRIM(installation_group) = ''
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE assets SET installation_group = ? WHERE id = ?",
            (infer_installation_group(row["project_name"]), row["id"]),
        )
