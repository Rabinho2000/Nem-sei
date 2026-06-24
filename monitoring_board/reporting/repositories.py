from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from monitoring_board.db import query_all
from monitoring_board.reporting.billing import decimal_from_value
from monitoring_board.reporting.models import (
    BillingConfig,
    BillingEnergyBase,
    BillingMode,
    HourlyEnergyRecord,
    InvoiceCandidate,
    InvoiceExtractionResult,
    InvoiceStatus,
    ReportType,
    TariffConfig,
    TariffPeriodRule,
    TariffType,
)
from monitoring_board.reporting.tariffs import (
    PERIOD_CHEIA,
    PERIOD_PONTA,
    PERIOD_SIMPLE,
    PERIOD_SUPER_VAZIO,
    PERIOD_VAZIO,
    TariffValidationError,
    decimal_from_tariff_value,
    parse_date_optional,
    parse_hhmm,
    parse_tariff_type,
    validate_tariff_config,
    validate_rules,
)


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


def create_source_file_record(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    portfolio_id: int | None,
    file_type: str,
    original_filename: str,
    stored_path: str,
    sha256: str = "",
    mime_type: str = "",
    size_bytes: int | None = None,
    notes: str = "",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO source_files (
            asset_id, portfolio_id, file_type, original_filename, stored_path, uploaded_at,
            notes, sha256, mime_type, size_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            portfolio_id,
            file_type,
            original_filename,
            stored_path,
            datetime.now().isoformat(timespec="seconds"),
            notes,
            sha256,
            mime_type,
            size_bytes,
        ),
    )
    return int(cursor.lastrowid)


def find_invoice_by_hash(conn: sqlite3.Connection, *, asset_id: int | None, sha256: str) -> sqlite3.Row | None:
    if asset_id is None:
        return conn.execute(
            """
            SELECT d.*
            FROM invoice_documents d
            WHERE d.sha256 = ?
            LIMIT 1
            """,
            (sha256,),
        ).fetchone()
    return conn.execute(
        """
        SELECT d.*
        FROM invoice_documents d
        WHERE d.asset_id = ? AND d.sha256 = ?
        LIMIT 1
        """,
        (asset_id, sha256),
    ).fetchone()


def create_invoice_document(
    conn: sqlite3.Connection,
    *,
    source_file_id: int,
    asset_id: int,
    sha256: str,
    mime_type: str,
    size_bytes: int,
    status: str = InvoiceStatus.UPLOADED.value,
    warnings: tuple[str, ...] = (),
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO invoice_documents (
            source_file_id, asset_id, status, sha256, mime_type, size_bytes,
            warnings_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_file_id) DO UPDATE SET
            status = excluded.status,
            sha256 = excluded.sha256,
            mime_type = excluded.mime_type,
            size_bytes = excluded.size_bytes,
            warnings_json = excluded.warnings_json,
            updated_at = excluded.updated_at
        """,
        (source_file_id, asset_id, status, sha256, mime_type, size_bytes, json.dumps(list(warnings), ensure_ascii=True), now, now),
    )
    row = conn.execute("SELECT id FROM invoice_documents WHERE source_file_id = ?", (source_file_id,)).fetchone()
    return int(row["id"] if row else cursor.lastrowid)


def ensure_invoice_document_for_source_file(conn: sqlite3.Connection, source_file_id: int) -> sqlite3.Row | None:
    existing = conn.execute("SELECT * FROM invoice_documents WHERE source_file_id = ?", (source_file_id,)).fetchone()
    if existing:
        return existing
    source = conn.execute("SELECT * FROM source_files WHERE id = ? AND file_type = 'invoice'", (source_file_id,)).fetchone()
    if source is None:
        return None
    document_id = create_invoice_document(
        conn,
        source_file_id=source_file_id,
        asset_id=int(source["asset_id"]),
        sha256=source["sha256"] if _row_has_key(source, "sha256") and source["sha256"] else "",
        mime_type=source["mime_type"] if _row_has_key(source, "mime_type") and source["mime_type"] else "",
        size_bytes=int(source["size_bytes"] or 0) if _row_has_key(source, "size_bytes") and source["size_bytes"] is not None else 0,
        status=InvoiceStatus.UPLOADED.value,
    )
    return get_invoice_document(conn, document_id)


def get_invoice_document(conn: sqlite3.Connection, invoice_document_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT d.*, sf.original_filename, sf.stored_path
        FROM invoice_documents d
        JOIN source_files sf ON sf.id = d.source_file_id
        WHERE d.id = ?
        """,
        (invoice_document_id,),
    ).fetchone()


def list_asset_invoice_documents(conn: sqlite3.Connection, asset_id: int) -> list[sqlite3.Row]:
    source_rows = query_all(conn, "SELECT id FROM source_files WHERE asset_id = ? AND file_type = 'invoice'", (asset_id,))
    for row in source_rows:
        ensure_invoice_document_for_source_file(conn, int(row["id"]))
    return query_all(
        conn,
        """
        SELECT d.*, sf.original_filename, sf.stored_path
        FROM invoice_documents d
        JOIN source_files sf ON sf.id = d.source_file_id
        WHERE d.asset_id = ? AND COALESCE(sf.archived_at, '') = ''
        ORDER BY d.created_at DESC, d.id DESC
        """,
        (asset_id,),
    )


def list_portfolio_invoice_documents(conn: sqlite3.Connection, portfolio_id: int) -> list[sqlite3.Row]:
    rows = query_all(
        conn,
        """
        SELECT DISTINCT pa.asset_id
        FROM portfolio_assets pa
        WHERE pa.portfolio_id = ? AND pa.asset_id IS NOT NULL
        """,
        (portfolio_id,),
    )
    documents: list[sqlite3.Row] = []
    for row in rows:
        documents.extend(list_asset_invoice_documents(conn, int(row["asset_id"])))
    return documents


def create_invoice_extraction_run(
    conn: sqlite3.Connection,
    *,
    invoice_document_id: int,
    result: InvoiceExtractionResult,
) -> int:
    fields = {candidate.field_name: candidate.value for candidate in result.candidates}
    confidence = {candidate.field_name: str(candidate.confidence) for candidate in result.candidates}
    evidence = {candidate.field_name: {"evidence": candidate.evidence, "source": candidate.source} for candidate in result.candidates}
    cursor = conn.execute(
        """
        INSERT INTO invoice_extraction_runs (
            invoice_document_id, parser_name, parser_version, status, extracted_values_json,
            confidence_json, evidence_json, warnings_json, error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_document_id,
            result.parser_name,
            result.parser_version,
            result.status.value,
            json.dumps(fields, ensure_ascii=True),
            json.dumps(confidence, ensure_ascii=True),
            json.dumps(evidence, ensure_ascii=True),
            json.dumps(list(result.warnings), ensure_ascii=True),
            "; ".join(result.errors),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return int(cursor.lastrowid)


def list_invoice_extraction_runs(conn: sqlite3.Connection, invoice_document_id: int) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT *
        FROM invoice_extraction_runs
        WHERE invoice_document_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (invoice_document_id,),
    )


def update_invoice_from_candidates(
    conn: sqlite3.Connection,
    *,
    invoice_document_id: int,
    candidates: tuple[InvoiceCandidate, ...],
    status: str,
    confidence: Decimal,
    warnings: tuple[str, ...] = (),
) -> None:
    values = {candidate.field_name: candidate.value for candidate in candidates}
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE invoice_documents
        SET status = ?, supplier_name = ?, supplier_nif = ?, customer_name = ?, customer_nif = ?,
            invoice_number = ?, issue_date = ?, billing_period_start = ?, billing_period_end = ?,
            currency = ?, total_amount = ?, total_energy_kwh = ?, tariff_type_candidate = ?,
            simple_price_eur_kwh = ?, ponta_price_eur_kwh = ?, cheia_price_eur_kwh = ?,
            vazio_price_eur_kwh = ?, super_vazio_price_eur_kwh = ?, extraction_method = ?,
            extraction_confidence = ?, warnings_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            values.get("supplier_name"),
            values.get("supplier_nif"),
            values.get("customer_name"),
            values.get("customer_nif"),
            values.get("invoice_number"),
            values.get("issue_date"),
            values.get("billing_period_start"),
            values.get("billing_period_end"),
            values.get("currency") or "EUR",
            values.get("total_amount"),
            values.get("total_energy_kwh"),
            values.get("tariff_type_candidate"),
            values.get("simple_price_eur_kwh"),
            values.get("ponta_price_eur_kwh"),
            values.get("cheia_price_eur_kwh"),
            values.get("vazio_price_eur_kwh"),
            values.get("super_vazio_price_eur_kwh"),
            "assisted",
            str(confidence),
            json.dumps(list(warnings), ensure_ascii=True),
            now,
            invoice_document_id,
        ),
    )


def update_invoice_review(
    conn: sqlite3.Connection,
    *,
    invoice_document_id: int,
    values: dict[str, Any],
    warnings: tuple[str, ...],
    status: str = InvoiceStatus.REVIEW_REQUIRED.value,
) -> None:
    candidates = tuple(
        InvoiceCandidate(field_name=key, value=str(value), confidence=Decimal("1"), evidence="manual_review", source="manual")
        for key, value in values.items()
        if value not in (None, "")
    )
    update_invoice_from_candidates(conn, invoice_document_id=invoice_document_id, candidates=candidates, status=status, confidence=Decimal("1"), warnings=warnings)
    conn.execute("UPDATE invoice_documents SET reviewed_at = ?, updated_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"), invoice_document_id))


def confirm_invoice_document(conn: sqlite3.Connection, invoice_document_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE invoice_documents SET status = ?, reviewed_at = ?, updated_at = ? WHERE id = ?", (InvoiceStatus.CONFIRMED.value, now, now, invoice_document_id))


def reject_invoice_document(conn: sqlite3.Connection, invoice_document_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE invoice_documents SET status = ?, reviewed_at = ?, updated_at = ? WHERE id = ?", (InvoiceStatus.REJECTED.value, now, now, invoice_document_id))


def archive_invoice_document(conn: sqlite3.Connection, invoice_document_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    row = get_invoice_document(conn, invoice_document_id)
    if row:
        conn.execute("UPDATE source_files SET archived_at = ? WHERE id = ?", (now, row["source_file_id"]))
    conn.execute("UPDATE invoice_documents SET status = ?, updated_at = ? WHERE id = ?", (InvoiceStatus.ARCHIVED.value, now, invoice_document_id))


def get_confirmed_invoice_for_tariff(conn: sqlite3.Connection, tariff_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT d.*
        FROM asset_tariffs t
        JOIN invoice_documents d ON d.source_file_id = t.invoice_file_id
        WHERE t.id = ? AND d.status = ?
        """,
        (tariff_id, InvoiceStatus.CONFIRMED.value),
    ).fetchone()


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


def list_tariffs_at(conn: sqlite3.Connection, *, asset_id: int, moment: date | datetime) -> list[sqlite3.Row]:
    day = moment.date() if isinstance(moment, datetime) else moment
    return query_all(
        conn,
        """
        SELECT *
        FROM asset_tariffs
        WHERE asset_id = ?
          AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?)
          AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)
        ORDER BY COALESCE(valid_from, ''), id
        """,
        (asset_id, day.isoformat(), day.isoformat()),
    )


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


def list_tariffs_intersecting_period(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    start: date,
    end: date,
) -> list[sqlite3.Row]:
    return query_all(
        conn,
        """
        SELECT *
        FROM asset_tariffs
        WHERE asset_id = ?
          AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?)
          AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)
        ORDER BY COALESCE(valid_from, ''), id
        """,
        (asset_id, end.isoformat(), start.isoformat()),
    )


def resolve_tariff_at(conn: sqlite3.Connection, *, asset_id: int, moment: date | datetime) -> sqlite3.Row | None:
    rows = list_tariffs_at(conn, asset_id=asset_id, moment=moment)
    return rows[0] if len(rows) == 1 else None


def detect_tariff_validity_warnings(conn: sqlite3.Connection, *, asset_id: int, start: date, end: date) -> tuple[str, ...]:
    tariffs = list_tariffs_intersecting_period(conn, asset_id=asset_id, start=start, end=end)
    warnings: set[str] = set()
    if not tariffs:
        warnings.add("missing_tariff")
        if has_expired_tariff(conn, asset_id, start):
            warnings.add("expired_tariff")
        return tuple(sorted(warnings))
    ordered: list[tuple[date, date]] = []
    for row in tariffs:
        row_start = parse_date_optional(row["valid_from"]) or date.min
        row_end = parse_date_optional(row["valid_to"]) or date.max
        ordered.append((max(row_start, start), min(row_end, end)))
    ordered.sort()
    cursor = start
    for row_start, row_end in ordered:
        if row_start > cursor:
            warnings.add("tariff_validity_gap")
        if row_start < cursor:
            warnings.add("overlapping_tariffs")
        if row_end >= cursor:
            cursor = end + timedelta(days=1) if row_end >= end else row_end + timedelta(days=1)
    if cursor <= end:
        warnings.add("tariff_validity_gap")
    return tuple(sorted(warnings))


def row_to_tariff_config(row: sqlite3.Row | dict[str, Any] | None, rules: list[sqlite3.Row | dict[str, Any]] | None = None) -> TariffConfig | None:
    if row is None:
        return None
    tariff_type = parse_tariff_type(_row_get(row, "tariff_type") or TariffType.SIMPLE.value)
    prices = {
        PERIOD_SIMPLE: decimal_from_tariff_value(_row_get(row, "simple_price_eur_kwh"), field_name="simple_price_eur_kwh"),
        PERIOD_PONTA: decimal_from_tariff_value(_row_get(row, "ponta_price_eur_kwh"), field_name="ponta_price_eur_kwh"),
        PERIOD_CHEIA: decimal_from_tariff_value(_row_get(row, "cheia_price_eur_kwh"), field_name="cheia_price_eur_kwh"),
        PERIOD_VAZIO: decimal_from_tariff_value(_row_get(row, "vazio_price_eur_kwh"), field_name="vazio_price_eur_kwh"),
        PERIOD_SUPER_VAZIO: decimal_from_tariff_value(_row_get(row, "super_vazio_price_eur_kwh"), field_name="super_vazio_price_eur_kwh"),
    }
    parsed_rules = tuple(row_to_tariff_rule(rule) for rule in (rules or []))
    return TariffConfig(
        tariff_id=int(_row_get(row, "id")) if _row_get(row, "id") is not None else None,
        asset_id=int(_row_get(row, "asset_id")) if _row_get(row, "asset_id") is not None else 0,
        tariff_type=tariff_type,
        cycle_type=str(_row_get(row, "cycle_type") or ""),
        valid_from=parse_date_optional(_row_get(row, "valid_from")),
        valid_to=parse_date_optional(_row_get(row, "valid_to")),
        prices=prices,
        rules=parsed_rules,
        invoice_file_id=int(_row_get(row, "invoice_file_id")) if _row_get(row, "invoice_file_id") is not None else None,
        notes=str(_row_get(row, "notes") or ""),
    )


def row_to_tariff_rule(row: sqlite3.Row | dict[str, Any]) -> TariffPeriodRule:
    return TariffPeriodRule(
        weekday_type=str(row["weekday_type"] or "all"),
        start_time=parse_hhmm(row["start_time"]),
        end_time=parse_hhmm(row["end_time"]),
        period_name=str(row["period_name"] or ""),
    )


def get_tariff_config_for_date(conn: sqlite3.Connection, *, asset_id: int, moment: date | datetime) -> TariffConfig | None:
    row = resolve_tariff_at(conn, asset_id=asset_id, moment=moment)
    if row is None:
        return None
    rules = list_tariff_period_rules(conn, int(row["id"]))
    return row_to_tariff_config(row, rules)


def get_tariff_resolution_warnings(conn: sqlite3.Connection, *, asset_id: int, moment: date | datetime) -> tuple[str, ...]:
    tariff_count = len(list_tariffs_at(conn, asset_id=asset_id, moment=moment))
    if tariff_count == 0:
        return ("missing_tariff",)
    if tariff_count > 1:
        return ("overlapping_tariffs",)
    return ()


def save_asset_tariff(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    tariff_type: str,
    cycle_type: str = "",
    simple_price_eur_kwh: Any = None,
    ponta_price_eur_kwh: Any = None,
    cheia_price_eur_kwh: Any = None,
    vazio_price_eur_kwh: Any = None,
    super_vazio_price_eur_kwh: Any = None,
    invoice_file_id: int | None = None,
    valid_from: str = "",
    valid_to: str = "",
    notes: str = "",
) -> int:
    if asset_id <= 0:
        raise TariffValidationError("Tarifa sem instalacao.")
    if invoice_file_id is not None:
        invoice = conn.execute("SELECT asset_id FROM source_files WHERE id = ? AND file_type = 'invoice'", (invoice_file_id,)).fetchone()
        if invoice is None or int(invoice["asset_id"]) != asset_id:
            raise TariffValidationError("Fatura associada pertence a outra instalacao.")
    config = TariffConfig(
        tariff_id=None,
        asset_id=asset_id,
        tariff_type=parse_tariff_type(tariff_type),
        cycle_type=cycle_type.strip(),
        valid_from=parse_date_optional(valid_from),
        valid_to=parse_date_optional(valid_to),
        prices={
            PERIOD_SIMPLE: decimal_from_tariff_value(simple_price_eur_kwh, field_name="simple_price_eur_kwh"),
            PERIOD_PONTA: decimal_from_tariff_value(ponta_price_eur_kwh, field_name="ponta_price_eur_kwh"),
            PERIOD_CHEIA: decimal_from_tariff_value(cheia_price_eur_kwh, field_name="cheia_price_eur_kwh"),
            PERIOD_VAZIO: decimal_from_tariff_value(vazio_price_eur_kwh, field_name="vazio_price_eur_kwh"),
            PERIOD_SUPER_VAZIO: decimal_from_tariff_value(super_vazio_price_eur_kwh, field_name="super_vazio_price_eur_kwh"),
        },
        invoice_file_id=invoice_file_id,
        notes=notes.strip(),
    )
    validate_tariff_config(config)
    cursor = conn.execute(
        """
        INSERT INTO asset_tariffs (
            asset_id, tariff_type, cycle_type, simple_price_eur_kwh, ponta_price_eur_kwh,
            cheia_price_eur_kwh, vazio_price_eur_kwh, super_vazio_price_eur_kwh,
            invoice_file_id, valid_from, valid_to, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            config.tariff_type.value,
            config.cycle_type,
            str(config.prices.get(PERIOD_SIMPLE)) if config.prices.get(PERIOD_SIMPLE) is not None else None,
            str(config.prices.get(PERIOD_PONTA)) if config.prices.get(PERIOD_PONTA) is not None else None,
            str(config.prices.get(PERIOD_CHEIA)) if config.prices.get(PERIOD_CHEIA) is not None else None,
            str(config.prices.get(PERIOD_VAZIO)) if config.prices.get(PERIOD_VAZIO) is not None else None,
            str(config.prices.get(PERIOD_SUPER_VAZIO)) if config.prices.get(PERIOD_SUPER_VAZIO) is not None else None,
            invoice_file_id,
            config.valid_from.isoformat() if config.valid_from else "",
            config.valid_to.isoformat() if config.valid_to else "",
            config.notes,
        ),
    )
    return int(cursor.lastrowid)


def add_tariff_period_rule(
    conn: sqlite3.Connection,
    *,
    tariff_id: int,
    weekday_type: str,
    start_time: str,
    end_time: str,
    period_name: str,
) -> int:
    tariff = conn.execute("SELECT * FROM asset_tariffs WHERE id = ?", (tariff_id,)).fetchone()
    if tariff is None:
        raise TariffValidationError("Tarifa inexistente.")
    candidate = TariffPeriodRule(weekday_type=weekday_type.strip(), start_time=parse_hhmm(start_time), end_time=parse_hhmm(end_time), period_name=period_name.strip())
    existing = [row_to_tariff_rule(row) for row in list_tariff_period_rules(conn, tariff_id)]
    tariff_type = parse_tariff_type(tariff["tariff_type"])
    validate_rules([*existing, candidate], tariff_type)
    cursor = conn.execute(
        "INSERT INTO tariff_period_rules (tariff_id, weekday_type, start_time, end_time, period_name) VALUES (?, ?, ?, ?, ?)",
        (tariff_id, candidate.weekday_type, candidate.start_time.strftime("%H:%M"), candidate.end_time.strftime("%H:%M"), candidate.period_name),
    )
    return int(cursor.lastrowid)


def delete_tariff_period_rule(conn: sqlite3.Connection, *, rule_id: int, asset_id: int | None = None) -> bool:
    row = conn.execute(
        """
        SELECT r.id
        FROM tariff_period_rules r
        JOIN asset_tariffs t ON t.id = r.tariff_id
        WHERE r.id = ? AND (? IS NULL OR t.asset_id = ?)
        """,
        (rule_id, asset_id, asset_id),
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM tariff_period_rules WHERE id = ?", (rule_id,))
    return True


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
        SELECT *
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


def upsert_hourly_energy_record(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    provider: str,
    period_start: datetime,
    period_end: datetime,
    production_kwh: Any = None,
    self_use_kwh: Any = None,
    export_kwh: Any = None,
    consumption_kwh: Any = None,
    grid_import_kwh: Any = None,
    payload_json: str | dict[str, Any] | None = None,
    data_quality: str = "ok",
    source_fields: dict[str, Any] | None = None,
) -> None:
    payload_text = json.dumps(payload_json, ensure_ascii=True) if isinstance(payload_json, dict) else (payload_json or "{}")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO production_hourly_records (
            asset_id, provider, period_start, period_end, production_kwh, self_use_kwh,
            export_kwh, consumption_kwh, grid_import_kwh, payload_json, imported_at,
            data_quality, source_fields_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id, provider, period_start) DO UPDATE SET
            period_end = excluded.period_end,
            production_kwh = excluded.production_kwh,
            self_use_kwh = excluded.self_use_kwh,
            export_kwh = excluded.export_kwh,
            consumption_kwh = excluded.consumption_kwh,
            grid_import_kwh = excluded.grid_import_kwh,
            payload_json = excluded.payload_json,
            imported_at = excluded.imported_at,
            data_quality = excluded.data_quality,
            source_fields_json = excluded.source_fields_json
        """,
        (
            asset_id,
            provider,
            period_start.isoformat(timespec="seconds"),
            period_end.isoformat(timespec="seconds"),
            float(decimal_from_value(production_kwh)) if production_kwh is not None else None,
            float(decimal_from_value(self_use_kwh)) if self_use_kwh is not None else None,
            float(decimal_from_value(export_kwh)) if export_kwh is not None else None,
            float(decimal_from_value(consumption_kwh)) if consumption_kwh is not None else None,
            float(decimal_from_value(grid_import_kwh)) if grid_import_kwh is not None else None,
            payload_text,
            now,
            data_quality,
            json.dumps(source_fields or {}, ensure_ascii=True),
        ),
    )


def row_to_hourly_energy_record(row: sqlite3.Row | dict[str, Any]) -> HourlyEnergyRecord:
    source_fields: dict[str, str] | None = None
    raw_source_fields = None
    try:
        raw_source_fields = row["source_fields_json"]
    except (KeyError, IndexError):
        raw_source_fields = None
    if raw_source_fields:
        try:
            parsed = json.loads(raw_source_fields)
            if isinstance(parsed, dict):
                source_fields = {str(key): str(value) for key, value in parsed.items()}
        except json.JSONDecodeError:
            source_fields = None
    return HourlyEnergyRecord(
        period_start=datetime.fromisoformat(str(row["period_start"])),
        period_end=(
            datetime.fromisoformat(str(row["period_end"]))
            if _row_has_key(row, "period_end") and row["period_end"]
            else datetime.fromisoformat(str(row["period_start"])) + timedelta(hours=1)
        ),
        production_kwh=decimal_from_value(row["production_kwh"]) if row["production_kwh"] is not None else None,
        self_use_kwh=decimal_from_value(row["self_use_kwh"]) if _row_has_key(row, "self_use_kwh") and row["self_use_kwh"] is not None else None,
        export_kwh=decimal_from_value(row["export_kwh"]) if _row_has_key(row, "export_kwh") and row["export_kwh"] is not None else None,
        consumption_kwh=decimal_from_value(row["consumption_kwh"]) if _row_has_key(row, "consumption_kwh") and row["consumption_kwh"] is not None else None,
        grid_import_kwh=decimal_from_value(row["grid_import_kwh"]) if _row_has_key(row, "grid_import_kwh") and row["grid_import_kwh"] is not None else None,
        data_quality=str(row["data_quality"]) if _row_has_key(row, "data_quality") and row["data_quality"] else None,
        source_fields=source_fields,
    )


def _row_has_key(row: sqlite3.Row | dict[str, Any], key: str) -> bool:
    return key in row.keys() if hasattr(row, "keys") else key in row


def _row_get(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
    return row[key] if _row_has_key(row, key) else default


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
