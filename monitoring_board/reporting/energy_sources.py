from __future__ import annotations

import sqlite3
from typing import Any


def resolve_asset_energy_source(
    conn: sqlite3.Connection,
    asset_id: int | None,
) -> dict[str, Any] | None:
    """Return the single integration used as the asset's reporting source.

    An explicitly selected source wins. Existing installations remain
    compatible by preferring FusionSolar when no source has been selected yet.
    """

    if asset_id is None:
        return None
    row = conn.execute(
        """
        SELECT
            ai.id,
            ai.asset_id,
            ai.provider,
            ai.external_id,
            ai.external_name,
            ai.is_primary_energy_source
        FROM asset_integrations ai
        WHERE ai.asset_id = ?
          AND ai.enabled = 1
          AND COALESCE(ai.external_id, '') != ''
        ORDER BY
            COALESCE(ai.is_primary_energy_source, 0) DESC,
            CASE WHEN ai.provider = 'FusionSolar' THEN 0 ELSE 1 END,
            ai.id
        LIMIT 1
        """,
        (asset_id,),
    ).fetchone()
    if row is not None:
        return dict(row)
    # Compatibility for historical databases and imported report fixtures that
    # already contain persisted energy facts but predate asset mappings.
    legacy = conn.execute(
        """
        SELECT provider, MAX(external_id) AS external_id
        FROM (
            SELECT provider, external_id
            FROM production_records
            WHERE asset_id = ?
            UNION ALL
            SELECT provider, '' AS external_id
            FROM production_hourly_records
            WHERE asset_id = ?
        )
        GROUP BY provider
        ORDER BY CASE WHEN provider = 'FusionSolar' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (asset_id, asset_id),
    ).fetchone()
    if legacy is None:
        return None
    return {
        "id": None,
        "asset_id": asset_id,
        "provider": legacy["provider"],
        "external_id": legacy["external_id"],
        "external_name": legacy["external_id"],
        "is_primary_energy_source": 0,
    }


def resolve_asset_energy_provider(
    conn: sqlite3.Connection,
    asset_id: int | None,
) -> str | None:
    source = resolve_asset_energy_source(conn, asset_id)
    return str(source["provider"]) if source is not None else None


def set_asset_primary_energy_source(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
) -> None:
    selected = conn.execute(
        """
        SELECT id
        FROM asset_integrations
        WHERE asset_id = ? AND provider = ? AND enabled = 1
          AND COALESCE(external_id, '') != ''
        LIMIT 1
        """,
        (asset_id, provider),
    ).fetchone()
    if selected is None:
        raise ValueError("A fonte escolhida nao tem um mapeamento ativo para esta instalacao.")
    conn.execute(
        "UPDATE asset_integrations SET is_primary_energy_source = 0 WHERE asset_id = ?",
        (asset_id,),
    )
    conn.execute(
        "UPDATE asset_integrations SET is_primary_energy_source = 1 WHERE id = ?",
        (int(selected["id"]),),
    )
