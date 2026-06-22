from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from monitoring_board.db import query_all
from monitoring_board.reporting.billing import decimal_from_value
from monitoring_board.reporting.models import BillingConfig, BillingEnergyBase, BillingMode, ReportType


def ensure_billing_config_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_billing_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL UNIQUE,
            billing_mode TEXT NOT NULL DEFAULT 'energy',
            billing_energy_base TEXT NOT NULL DEFAULT 'self_consumption',
            solcor_price_per_kwh TEXT NOT NULL DEFAULT '0',
            fixed_monthly_fee_eur TEXT NOT NULL DEFAULT '0',
            default_electricity_price TEXT NOT NULL DEFAULT '0',
            default_export_price TEXT NOT NULL DEFAULT '0',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
        )
        """
    )


def default_billing_config(report_type: ReportType) -> BillingConfig:
    return BillingConfig(report_type=report_type)


def row_to_billing_config(row: sqlite3.Row | dict[str, Any] | None, report_type: ReportType) -> BillingConfig:
    if row is None:
        return default_billing_config(report_type)
    try:
        billing_mode = BillingMode(str(row["billing_mode"] or BillingMode.ENERGY.value))
    except ValueError:
        billing_mode = BillingMode.ENERGY
    try:
        billing_energy_base = BillingEnergyBase(str(row["billing_energy_base"] or BillingEnergyBase.SELF_CONSUMPTION.value))
    except ValueError:
        billing_energy_base = BillingEnergyBase.SELF_CONSUMPTION
    return BillingConfig(
        report_type=report_type,
        billing_mode=billing_mode,
        billing_energy_base=billing_energy_base,
        solcor_price_per_kwh=decimal_from_value(row["solcor_price_per_kwh"]),
        fixed_monthly_fee_eur=decimal_from_value(row["fixed_monthly_fee_eur"]),
        electricity_price_eur_kwh=decimal_from_value(row["default_electricity_price"]),
        export_price_eur_kwh=decimal_from_value(row["default_export_price"]),
    )


def get_asset_billing_config_row(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM asset_billing_configs WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()


def get_asset_billing_config(conn: sqlite3.Connection, asset_id: int, report_type: ReportType) -> BillingConfig:
    return row_to_billing_config(get_asset_billing_config_row(conn, asset_id), report_type)


def upsert_asset_billing_config(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    config: BillingConfig,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO asset_billing_configs (
            asset_id, billing_mode, billing_energy_base, solcor_price_per_kwh,
            fixed_monthly_fee_eur, default_electricity_price, default_export_price,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id) DO UPDATE SET
            billing_mode = excluded.billing_mode,
            billing_energy_base = excluded.billing_energy_base,
            solcor_price_per_kwh = excluded.solcor_price_per_kwh,
            fixed_monthly_fee_eur = excluded.fixed_monthly_fee_eur,
            default_electricity_price = excluded.default_electricity_price,
            default_export_price = excluded.default_export_price,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            config.billing_mode.value,
            config.billing_energy_base.value,
            str(config.solcor_price_per_kwh),
            str(config.fixed_monthly_fee_eur),
            str(config.electricity_price_eur_kwh),
            str(config.export_price_eur_kwh),
            now,
            now,
        ),
    )


def billing_config_to_form_values(config: BillingConfig) -> dict[str, str]:
    return {
        "billing_mode": config.billing_mode.value,
        "billing_energy_base": config.billing_energy_base.value,
        "solcor_price_per_kwh": str(config.solcor_price_per_kwh),
        "fixed_monthly_fee_eur": str(config.fixed_monthly_fee_eur),
        "electricity_price": str(config.electricity_price_eur_kwh),
        "sell_price": str(config.export_price_eur_kwh),
    }


def list_portfolio_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query_all(conn, "SELECT * FROM portfolio_groups ORDER BY name COLLATE NOCASE")


def get_latest_tariff(conn: sqlite3.Connection, asset_id: int, report_start: date) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM asset_tariffs
        WHERE asset_id = ?
          AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?)
          AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)
        ORDER BY COALESCE(valid_from, '') DESC, id DESC
        LIMIT 1
        """,
        (asset_id, report_start.isoformat(), report_start.isoformat()),
    ).fetchone()


def has_expired_tariff(conn: sqlite3.Connection, asset_id: int, report_start: date) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM asset_tariffs
        WHERE asset_id = ?
          AND valid_to IS NOT NULL
          AND valid_to != ''
          AND valid_to < ?
        LIMIT 1
        """,
        (asset_id, report_start.isoformat()),
    ).fetchone()
    return row is not None


def get_monthly_availability(conn: sqlite3.Connection, asset_id: int, start: date, end: date) -> float | None:
    row = conn.execute(
        """
        SELECT SUM(weighted_availability_pct * valid_slots) AS weighted_sum, SUM(valid_slots) AS slots
        FROM plant_availability_daily
        WHERE asset_id = ? AND provider = 'FusionSolar' AND availability_date BETWEEN ? AND ?
        """,
        (asset_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    if row and row["slots"]:
        return round(float(row["weighted_sum"]) / float(row["slots"]), 2)
    return None


def get_monthly_production_record(conn: sqlite3.Connection, asset_id: int | None, period_start: date) -> sqlite3.Row | None:
    if asset_id is None:
        return None
    return conn.execute(
        """
        SELECT production_kwh
        FROM production_records
        WHERE asset_id = ? AND provider = 'FusionSolar' AND period_type = 'month' AND period_date = ?
        LIMIT 1
        """,
        (asset_id, period_start.isoformat()),
    ).fetchone()


def list_monthly_production_records(
    conn: sqlite3.Connection,
    *,
    asset_id: int | None,
    start: date,
    end: date,
) -> list[sqlite3.Row]:
    if asset_id is None:
        return []
    return query_all(
        conn,
        """
        SELECT *
        FROM production_records
        WHERE asset_id = ?
          AND provider = 'FusionSolar'
          AND period_type = 'month'
          AND period_date BETWEEN ? AND ?
          AND production_kwh IS NOT NULL
        ORDER BY period_date
        """,
        (asset_id, start.isoformat(), end.isoformat()),
    )


def list_daily_production_records(
    conn: sqlite3.Connection,
    *,
    asset_id: int | None,
    start: date,
    end: date,
) -> list[sqlite3.Row]:
    if asset_id is None:
        return []
    return query_all(
        conn,
        """
        SELECT *
        FROM production_records
        WHERE asset_id = ?
          AND provider = 'FusionSolar'
          AND period_type = 'day'
          AND period_date BETWEEN ? AND ?
          AND production_kwh IS NOT NULL
        ORDER BY period_date
        """,
        (asset_id, start.isoformat(), end.isoformat()),
    )


def get_latest_helioscope_expected(conn: sqlite3.Connection, asset_id: int | None, month: int) -> sqlite3.Row | None:
    if asset_id is None:
        return None
    return conn.execute(
        """
        SELECT expected_kwh
        FROM helioscope_expected_production
        WHERE asset_id = ? AND month = ?
        ORDER BY imported_at DESC, id DESC
        LIMIT 1
        """,
        (asset_id, month),
    ).fetchone()


def list_tariff_period_rules(conn: sqlite3.Connection, tariff_id: int) -> list[sqlite3.Row]:
    return query_all(
        conn,
        "SELECT * FROM tariff_period_rules WHERE tariff_id = ? ORDER BY weekday_type, start_time",
        (tariff_id,),
    )


def list_hourly_production_records(
    conn: sqlite3.Connection,
    *,
    asset_id: int | None,
    start_iso: str,
    end_iso: str,
) -> list[sqlite3.Row]:
    if asset_id is None:
        return []
    return query_all(
        conn,
        """
        SELECT *
        FROM production_hourly_records
        WHERE asset_id = ? AND provider = 'FusionSolar' AND period_start >= ? AND period_start < ?
        ORDER BY period_start
        """,
        (asset_id, start_iso, end_iso),
    )


def list_portfolio_report_assets(conn: sqlite3.Connection, portfolio_id: int) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT pa.*, a.project_name, a.nif AS asset_nif, a.start_contract, a.mounting_date, a.kwp
        FROM portfolio_assets pa
        LEFT JOIN assets a ON a.id = pa.asset_id
        WHERE pa.portfolio_id = ? AND pa.active = 1
        ORDER BY COALESCE(pa.external_name, a.project_name) COLLATE NOCASE
        """,
        (portfolio_id,),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
