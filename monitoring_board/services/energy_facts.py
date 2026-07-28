from __future__ import annotations

import calendar
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


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
SIGENERGY_HISTORY_FIELD_MAP = {
    "powerGeneration": "production_kwh",
    "powerUse": "consumption_kwh",
    # The official contract calls powerOneself "Load green power consumption".
    # This is the onsite energy that balances load against grid import and is
    # therefore the value used by reports and billing. powerSelfConsumption is
    # a distinct generation-side total and remains preserved in payload_json.
    "powerOneself": "self_use_kwh",
    "powerToGrid": "export_kwh",
    "powerFromGrid": "grid_import_kwh",
    "esCharging": "battery_charge_kwh",
    "esDischarging": "battery_discharge_kwh",
}
SIGENERGY_REPORT_CORE_FIELDS = (
    "production_kwh",
    "consumption_kwh",
    "self_use_kwh",
    "export_kwh",
    "grid_import_kwh",
)
LISBON = ZoneInfo("Europe/Lisbon")


@dataclass(frozen=True)
class SigenergyHistoryFact:
    system_id: str
    period_date: date
    values: dict[str, float | None]
    data_quality: str
    payload: dict[str, Any]


def parse_sigenergy_daily_history(
    payload: dict[str, Any],
    *,
    system_id: str,
    period_date: date,
    confirmed_unit: str = "",
) -> SigenergyHistoryFact:
    """Parse only documented daily cumulative energy fields.

    The current Sigenergy history response does not carry a reliable unit in
    every payload.  Values are therefore accepted only when the payload itself
    says kWh or the administrator has explicitly confirmed kWh for this API
    contract.
    """

    if not isinstance(payload, dict):
        raise ValueError("O historico Sigenergy devolveu um payload invalido.")
    payload_unit = str(payload.get("unit") or "").strip()
    normalized_unit = (payload_unit or confirmed_unit).strip().lower()
    if normalized_unit != "kwh":
        raise ValueError(
            "A unidade do historico Sigenergy ainda nao foi confirmada como kWh."
        )
    values: dict[str, float | None] = {}
    for source_field, target_field in SIGENERGY_HISTORY_FIELD_MAP.items():
        raw_value = payload.get(source_field)
        if raw_value in (None, ""):
            values[target_field] = None
            continue
        if isinstance(raw_value, bool):
            raise ValueError(f"{source_field} nao contem um valor energetico valido.")
        try:
            parsed = float(str(raw_value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{source_field} nao contem um valor energetico valido."
            ) from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{source_field} nao contem energia kWh valida.")
        values[target_field] = parsed
    available = [value for value in values.values() if value is not None]
    if not available:
        quality = "missing"
    elif all(values.get(field) is not None for field in SIGENERGY_REPORT_CORE_FIELDS):
        quality = "complete"
    else:
        quality = "partial"
    return SigenergyHistoryFact(
        system_id=system_id.strip(),
        period_date=period_date,
        values=values,
        data_quality=quality,
        payload=payload,
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


def persist_sigenergy_daily_history(
    conn: sqlite3.Connection,
    *,
    asset_id: int | None,
    fact: SigenergyHistoryFact,
) -> int:
    period_start = datetime.combine(
        fact.period_date,
        datetime.min.time(),
        tzinfo=LISBON,
    )
    period_end = period_start + timedelta(days=1)
    fact_id = upsert_energy_interval_fact(
        conn,
        asset_id=asset_id,
        provider="Sigenergy",
        external_id=fact.system_id,
        period_start=period_start,
        period_end=period_end,
        granularity="day",
        provenance="sigenergy_system_history",
        data_quality=fact.data_quality,
        timezone_name="Europe/Lisbon",
        source_event_id=f"history:{fact.system_id}:{fact.period_date.isoformat()}:day",
        payload=fact.payload,
        **fact.values,
    )
    if asset_id is not None:
        materialize_daily_energy_fact(
            conn,
            fact_id=fact_id,
            asset_id=asset_id,
        )
        materialize_energy_month(
            conn,
            asset_id=asset_id,
            provider="Sigenergy",
            month_start=fact.period_date.replace(day=1),
        )
    return fact_id


def materialize_daily_energy_fact(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    asset_id: int,
) -> None:
    fact = conn.execute(
        "SELECT * FROM energy_interval_facts WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if fact is None:
        raise ValueError("Facto energetico nao encontrado.")
    period_date = str(fact["period_start"])[:10]
    now = datetime.now(LISBON).isoformat(timespec="seconds")
    conn.execute(
        f"""
        INSERT INTO production_records (
            asset_id, provider, external_id, period_type, period_date,
            {", ".join(ENERGY_FIELDS)}, source_timezone, source_granularity,
            provenance, data_quality, payload_json, created_at, updated_at
        ) VALUES (
            ?, ?, ?, 'day', ?, {", ".join("?" for _ in ENERGY_FIELDS)},
            ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(asset_id, provider, period_type, period_date)
        DO UPDATE SET
            external_id = excluded.external_id,
            {", ".join(f"{field} = excluded.{field}" for field in ENERGY_FIELDS)},
            source_timezone = excluded.source_timezone,
            source_granularity = excluded.source_granularity,
            provenance = excluded.provenance,
            data_quality = excluded.data_quality,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            fact["provider"],
            fact["external_id"],
            period_date,
            *(fact[field] for field in ENERGY_FIELDS),
            fact["timezone"],
            fact["granularity"],
            fact["provenance"],
            fact["data_quality"],
            json.dumps(
                {
                    "energy_interval_fact_id": fact_id,
                    "source_event_id": fact["source_event_id"],
                },
                ensure_ascii=True,
            ),
            now,
            now,
        ),
    )


def materialize_energy_month(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
    month_start: date,
    reference_date: date | None = None,
) -> str:
    month_start = month_start.replace(day=1)
    expected_days = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = month_start.replace(day=expected_days)
    rows = conn.execute(
        """
        SELECT *
        FROM production_records
        WHERE asset_id = ? AND provider = ? AND period_type = 'day'
          AND period_date BETWEEN ? AND ?
        ORDER BY period_date
        """,
        (
            asset_id,
            provider,
            month_start.isoformat(),
            month_end.isoformat(),
        ),
    ).fetchall()
    distinct_dates = {str(row["period_date"]) for row in rows}
    current_month = (reference_date or datetime.now(LISBON).date()).replace(day=1)
    if month_start >= current_month:
        quality = "in_progress" if rows else "missing"
    elif not rows:
        quality = "missing"
    elif (
        len(distinct_dates) == expected_days
        and all(str(row["data_quality"] or "") == "complete" for row in rows)
    ):
        quality = "complete"
    else:
        quality = "partial"

    totals: dict[str, float | None] = {}
    for field in ENERGY_FIELDS:
        values = [row[field] for row in rows]
        totals[field] = (
            round(sum(float(value) for value in values), 9)
            if len(values) == expected_days and all(value is not None for value in values)
            else None
        )
    external_ids = {
        str(row["external_id"] or "").strip()
        for row in rows
        if str(row["external_id"] or "").strip()
    }
    external_id = next(iter(external_ids)) if len(external_ids) == 1 else ""
    if len(external_ids) > 1:
        quality = "conflict"
    now = datetime.now(LISBON).isoformat(timespec="seconds")
    conn.execute(
        f"""
        INSERT INTO production_records (
            asset_id, provider, external_id, period_type, period_date,
            {", ".join(ENERGY_FIELDS)}, source_timezone, source_granularity,
            provenance, data_quality, payload_json, created_at, updated_at
        ) VALUES (
            ?, ?, ?, 'month', ?, {", ".join("?" for _ in ENERGY_FIELDS)},
            'Europe/Lisbon', 'day', 'energy_interval_facts', ?, ?, ?, ?
        )
        ON CONFLICT(asset_id, provider, period_type, period_date)
        DO UPDATE SET
            external_id = excluded.external_id,
            {", ".join(f"{field} = excluded.{field}" for field in ENERGY_FIELDS)},
            source_timezone = excluded.source_timezone,
            source_granularity = excluded.source_granularity,
            provenance = excluded.provenance,
            data_quality = excluded.data_quality,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            provider,
            external_id,
            month_start.isoformat(),
            *(totals[field] for field in ENERGY_FIELDS),
            quality,
            json.dumps(
                {
                    "expected_days": expected_days,
                    "available_days": len(distinct_dates),
                    "coverage_ratio": (
                        len(distinct_dates) / expected_days if expected_days else 0
                    ),
                    "source": "energy_interval_facts",
                },
                ensure_ascii=True,
            ),
            now,
            now,
        ),
    )
    return quality


def sigenergy_energy_readiness(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    asset_id: int | None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    reference_date = reference_date or datetime.now(LISBON).date()
    month_start = reference_date.replace(day=1)
    expected_days = calendar.monthrange(month_start.year, month_start.month)[1]
    summary = conn.execute(
        """
        SELECT
            MIN(period_start) AS first_period_start,
            MAX(period_end) AS last_period_end,
            MAX(updated_at) AS last_event_at,
            MAX(granularity) AS granularity,
            COUNT(DISTINCT CASE
                WHEN substr(period_start, 1, 7) = ? THEN substr(period_start, 1, 10)
            END) AS current_month_days
        FROM energy_interval_facts
        WHERE provider = 'Sigenergy' AND external_id = ?
        """,
        (month_start.strftime("%Y-%m"), external_id),
    ).fetchone()
    ready_row = None
    if asset_id is not None:
        ready_row = conn.execute(
            """
            SELECT period_date
            FROM production_records
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND period_type = 'month' AND data_quality = 'complete'
              AND production_kwh IS NOT NULL
              AND consumption_kwh IS NOT NULL
              AND self_use_kwh IS NOT NULL
              AND export_kwh IS NOT NULL
              AND grid_import_kwh IS NOT NULL
            ORDER BY period_date
            LIMIT 1
            """,
            (asset_id,),
        ).fetchone()
    current_days = int(summary["current_month_days"] or 0) if summary else 0
    return {
        "report_ready": ready_row is not None,
        "report_ready_since": str(ready_row["period_date"]) if ready_row else "",
        "first_period_start": str(summary["first_period_start"] or "") if summary else "",
        "last_period_end": str(summary["last_period_end"] or "") if summary else "",
        "last_energy_event_at": str(summary["last_event_at"] or "") if summary else "",
        "granularity": str(summary["granularity"] or "") if summary else "",
        "current_month_days": current_days,
        "current_month_expected_days": expected_days,
        "current_month_coverage_pct": round(
            current_days / expected_days * 100,
            2,
        )
        if expected_days
        else 0.0,
    }
