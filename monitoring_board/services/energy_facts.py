from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any


ENERGY_QUALITY_STATES = {
    "complete",
    "partial",
    "missing",
    "conflict",
    "in_progress",
}
ENERGY_FIELDS = (
    "production_kwh",
    "consumption_kwh",
    "self_use_kwh",
    "export_kwh",
    "grid_import_kwh",
    "battery_charge_kwh",
    "battery_discharge_kwh",
    "ev_charge_kwh",
    "heat_pump_kwh",
)


def upsert_energy_interval_fact(
    conn: sqlite3.Connection,
    *,
    asset_id: int | None,
    provider: str,
    external_id: str,
    period_start: datetime,
    period_end: datetime,
    granularity: str,
    provenance: str,
    data_quality: str,
    timezone_name: str = "Europe/Lisbon",
    source_event_id: str = "",
    payload: dict[str, Any] | None = None,
    **energy_values: Any,
) -> int:
    if period_end <= period_start:
        raise ValueError("O fim do intervalo energetico tem de ser posterior ao inicio.")
    if data_quality not in ENERGY_QUALITY_STATES:
        raise ValueError("Qualidade energetica invalida.")
    if not provider.strip() or not external_id.strip() or not provenance.strip():
        raise ValueError("Provider, ID externo e proveniencia sao obrigatorios.")
    unknown = set(energy_values) - set(ENERGY_FIELDS)
    if unknown:
        raise ValueError(f"Campos energeticos desconhecidos: {', '.join(sorted(unknown))}")
    normalized: dict[str, float | None] = {}
    for field in ENERGY_FIELDS:
        value = energy_values.get(field)
        if value is None:
            normalized[field] = None
            continue
        parsed = float(value)
        if parsed < 0:
            raise ValueError(f"{field} nao pode ser negativo.")
        normalized[field] = parsed
    if data_quality == "complete" and not any(
        normalized[field] is not None for field in ENERGY_FIELDS
    ):
        raise ValueError("Um facto completo tem de incluir pelo menos um valor de energia.")

    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO energy_interval_facts (
            asset_id, provider, external_id, period_start, period_end,
            timezone, granularity, production_kwh, consumption_kwh,
            self_use_kwh, export_kwh, grid_import_kwh, battery_charge_kwh,
            battery_discharge_kwh, ev_charge_kwh, heat_pump_kwh,
            provenance, data_quality, source_event_id, payload_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, external_id, period_start, period_end, provenance)
        DO UPDATE SET
            asset_id = excluded.asset_id,
            timezone = excluded.timezone,
            granularity = excluded.granularity,
            production_kwh = excluded.production_kwh,
            consumption_kwh = excluded.consumption_kwh,
            self_use_kwh = excluded.self_use_kwh,
            export_kwh = excluded.export_kwh,
            grid_import_kwh = excluded.grid_import_kwh,
            battery_charge_kwh = excluded.battery_charge_kwh,
            battery_discharge_kwh = excluded.battery_discharge_kwh,
            ev_charge_kwh = excluded.ev_charge_kwh,
            heat_pump_kwh = excluded.heat_pump_kwh,
            data_quality = excluded.data_quality,
            source_event_id = excluded.source_event_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            provider.strip(),
            external_id.strip(),
            period_start.isoformat(timespec="seconds"),
            period_end.isoformat(timespec="seconds"),
            timezone_name,
            granularity,
            *(normalized[field] for field in ENERGY_FIELDS),
            provenance.strip(),
            data_quality,
            source_event_id.strip() or None,
            json.dumps(payload or {}, ensure_ascii=True),
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT id
        FROM energy_interval_facts
        WHERE provider = ? AND external_id = ? AND period_start = ?
          AND period_end = ? AND provenance = ?
        """,
        (
            provider.strip(),
            external_id.strip(),
            period_start.isoformat(timespec="seconds"),
            period_end.isoformat(timespec="seconds"),
            provenance.strip(),
        ),
    ).fetchone()
    return int(row["id"])
