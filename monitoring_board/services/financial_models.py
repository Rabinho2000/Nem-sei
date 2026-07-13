from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from werkzeug.utils import secure_filename

from monitoring_board.db import query_all
from monitoring_board.financial_model_repository import (
    active_model_for_month,
    archive_model,
    cancel_model,
    confirm_model,
    create_preview_model,
    find_model_by_hash,
    get_active_model,
    get_asset,
    get_asset_model,
    get_model_source,
    list_asset_models,
    list_model_monthly,
    model_details,
    model_validation,
    model_warnings,
    replace_monthly_rows,
    update_model_parse_details,
)
from monitoring_board.portfolio_management import normalize_name, normalize_nif
from monitoring_board.reporting.financial_models import parse_financial_model_workbook
from monitoring_board.runtime import UPLOAD_DIR, path_is_within, store_runtime_relative_path


ALLOWED_SUFFIXES = {".xlsx", ".xlsm"}
FINANCIAL_UPLOAD_DIR = UPLOAD_DIR / "financial_models"
BLOCKING_VALIDATION_CODES = {"financial_model_nif_mismatch", "financial_model_name_mismatch"}


class FinancialModelError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_openxml_workbook(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            if "[Content_Types].xml" not in archive.namelist() or not any(name.startswith("xl/") for name in archive.namelist()):
                raise FinancialModelError("workbook_invalid")
    except BadZipFile as exc:
        raise FinancialModelError("workbook_invalid") from exc


def create_financial_model_preview(
    conn: sqlite3.Connection,
    *,
    upload_dir: Path,
    file_storage: Any,
    asset_id: int,
    base_year: int | None = None,
) -> int:
    asset = get_asset(conn, asset_id)
    if asset is None:
        raise FinancialModelError("asset_not_found")
    original = Path(file_storage.filename or "").name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise FinancialModelError("unsupported_financial_model_extension")
    safe_name = secure_filename(original)
    if not safe_name:
        raise FinancialModelError("invalid_filename")
    target_dir = upload_dir / "financial_models" / str(asset_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = target_dir / f".upload_{os.getpid()}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{suffix}"
    final_path: Path | None = None
    source_id: int | None = None
    try:
        file_storage.save(tmp_path)
        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise FinancialModelError("empty_financial_model_file")
        assert_openxml_workbook(tmp_path)
        file_hash = sha256_file(tmp_path)
        duplicate = find_model_by_hash(conn, asset_id=asset_id, sha256=file_hash)
        if duplicate is not None:
            raise FinancialModelError("duplicate_financial_model_upload")
        parsed = parse_financial_model_workbook(tmp_path)
        effective_year = int(base_year or parsed.base_year or 0)
        if effective_year <= 0:
            raise FinancialModelError("financial_model_missing_year")
        validation = validate_financial_model_for_asset(conn, asset=asset, parsed=parsed)
        final_path = target_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file_hash[:12]}_{safe_name}"
        if not path_is_within(final_path, target_dir):
            raise FinancialModelError("invalid_upload_path")
        shutil.move(str(tmp_path), str(final_path))
        try:
            stored_path = store_runtime_relative_path(final_path)
        except ValueError:
            stored_path = str(final_path)
        source_id = _create_source_file(
            conn,
            asset_id=asset_id,
            original_filename=original,
            stored_path=stored_path,
            sha256=file_hash,
            mime_type=mimetypes.guess_type(original)[0] or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=final_path.stat().st_size,
        )
        model_id = create_preview_model(
            conn,
            source_file_id=source_id,
            asset_id=asset_id,
            base_year=effective_year,
            detected_name=parsed.detected_name,
            detected_nif=parsed.detected_nif,
            detected_kwp=parsed.detected_kwp,
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            file_sha256=file_hash,
            warnings=list(parsed.warnings),
            validation=validation,
            details=parsed.details,
        )
        replace_monthly_rows(conn, model_id=model_id, asset_id=asset_id, base_year=effective_year, rows=list(parsed.monthly))
        return model_id
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        if final_path and final_path.exists() and source_id is None:
            final_path.unlink(missing_ok=True)
        raise


def _create_source_file(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    original_filename: str,
    stored_path: str,
    sha256: str,
    mime_type: str,
    size_bytes: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO source_files (
            asset_id, portfolio_id, file_type, original_filename, stored_path, uploaded_at,
            notes, sha256, mime_type, size_bytes
        ) VALUES (?, NULL, 'financial_model', ?, ?, ?, '', ?, ?, ?)
        """,
        (asset_id, original_filename, stored_path, datetime.now().isoformat(timespec="seconds"), sha256, mime_type, size_bytes),
    )
    return int(cursor.lastrowid)


def validate_financial_model_for_asset(conn: sqlite3.Connection, *, asset: sqlite3.Row, parsed: Any) -> dict[str, Any]:
    warnings: list[str] = []
    blocking: list[str] = []
    asset_nif = normalize_nif(asset["nif"] if "nif" in asset.keys() else "")
    detected_nif = normalize_nif(parsed.detected_nif)
    if asset_nif and detected_nif and asset_nif != detected_nif:
        warnings.append("financial_model_nif_mismatch")
        blocking.append("financial_model_nif_mismatch")
    elif not asset_nif or not detected_nif:
        warnings.append("financial_model_missing_nif")
    aliases = {
        normalize_name(row["alias_name"])
        for row in query_all(conn, "SELECT alias_name FROM asset_aliases WHERE asset_id = ? AND COALESCE(active, 1) = 1", (asset["id"],))
    }
    asset_name = normalize_name(asset["project_name"])
    detected_name = normalize_name(parsed.detected_name)
    if detected_name and detected_name not in {asset_name, *aliases} and asset_name not in detected_name and detected_name not in asset_name:
        warnings.append("financial_model_name_mismatch")
        blocking.append("financial_model_name_mismatch")
    asset_kwp = _float_or_none(asset["kwp"] if "kwp" in asset.keys() else None)
    if asset_kwp is None or parsed.detected_kwp is None:
        warnings.append("financial_model_missing_kwp")
    elif asset_kwp > 0 and abs(float(parsed.detected_kwp) - asset_kwp) / asset_kwp > 0.05:
        warnings.append("financial_model_kwp_mismatch")
    if parsed.base_year is None:
        warnings.append("financial_model_missing_year")
    return {"warnings": sorted(set(warnings)), "blocking": sorted(set(blocking))}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def confirm_financial_model_import(
    conn: sqlite3.Connection,
    *,
    model_id: int,
    asset_id: int,
    override: bool = False,
    override_reason: str = "",
) -> int:
    model = get_asset_model(conn, asset_id=asset_id, model_id=model_id)
    if model is None:
        raise FinancialModelError("financial_model_not_found")
    if model["status"] != "preview":
        raise FinancialModelError("financial_model_not_preview")
    validation = model_validation(model)
    blocking = set(validation.get("blocking") or [])
    if blocking and (not override or not override_reason.strip()):
        raise FinancialModelError("financial_model_override_required")
    source = get_model_source(conn, model_id)
    if source is None:
        raise FinancialModelError("financial_model_source_missing")
    path = resolve_financial_model_path(source)
    if sha256_file(path) != model["file_sha256"]:
        raise FinancialModelError("financial_model_hash_changed")
    parsed = parse_financial_model_workbook(path)
    asset = get_asset(conn, asset_id)
    if asset is None:
        raise FinancialModelError("asset_not_found")
    validation = validate_financial_model_for_asset(conn, asset=asset, parsed=parsed)
    blocking = set(validation.get("blocking") or [])
    if blocking and (not override or not override_reason.strip()):
        raise FinancialModelError("financial_model_override_required")
    update_model_parse_details(conn, model_id=model_id, warnings=list(parsed.warnings), validation=validation, details=parsed.details)
    replace_monthly_rows(conn, model_id=model_id, asset_id=asset_id, base_year=int(model["base_year"]), rows=list(parsed.monthly))
    return confirm_model(conn, model_id=model_id, override_reason=override_reason if override else "")


def cancel_financial_model_preview(conn: sqlite3.Connection, *, model_id: int, asset_id: int) -> None:
    if get_asset_model(conn, asset_id=asset_id, model_id=model_id) is None:
        raise FinancialModelError("financial_model_not_found")
    cancel_model(conn, model_id=model_id)


def archive_financial_model(conn: sqlite3.Connection, *, model_id: int, asset_id: int) -> None:
    if get_asset_model(conn, asset_id=asset_id, model_id=model_id) is None:
        raise FinancialModelError("financial_model_not_found")
    archive_model(conn, model_id=model_id)


def activate_financial_model(conn: sqlite3.Connection, *, model_id: int, asset_id: int) -> None:
    if get_asset_model(conn, asset_id=asset_id, model_id=model_id) is None:
        raise FinancialModelError("financial_model_not_found")
    from monitoring_board.financial_model_repository import activate_model

    activate_model(conn, model_id=model_id)


def resolve_financial_model_path(source: sqlite3.Row) -> Path:
    path = Path(source["stored_path"])
    candidate = path if path.is_absolute() else UPLOAD_DIR.parent / path
    if not candidate.exists():
        candidate = UPLOAD_DIR / "financial_models" / str(source["asset_id"]) / Path(source["stored_path"]).name
    resolved = candidate.resolve()
    allowed = (UPLOAD_DIR / "financial_models").resolve()
    test_or_custom_allowed = path.is_absolute() and "financial_models" in resolved.parts and str(source["asset_id"]) in resolved.parts
    if resolved.is_symlink() or (not path_is_within(resolved, allowed) and not test_or_custom_allowed):
        raise FinancialModelError("financial_model_path_invalid")
    return resolved


def build_asset_financial_model_context(conn: sqlite3.Connection, *, asset_id: int) -> dict[str, Any]:
    active = get_active_model(conn, asset_id=asset_id)
    versions = list_asset_models(conn, asset_id=asset_id)
    active_monthly = list_model_monthly(conn, model_id=int(active["id"])) if active else []
    comparison_rows = build_monthly_comparison(conn, asset_id=asset_id, model_id=int(active["id"])) if active else []
    return {
        "active": active,
        "active_monthly": active_monthly,
        "versions": versions,
        "warnings": model_warnings(active),
        "validation": model_validation(active),
        "details": model_details(active),
        "annual": annual_totals(active_monthly),
        "comparison_rows": comparison_rows,
        "chart_series": build_chart_series(comparison_rows),
    }


def annual_totals(rows: list[sqlite3.Row]) -> dict[str, float | None]:
    keys = (
        "expected_production_kwh",
        "expected_consumption_kwh",
        "expected_self_use_kwh",
        "expected_export_kwh",
        "expected_grid_import_kwh",
    )
    totals: dict[str, float | None] = {}
    for key in keys:
        values = [row[key] for row in rows if row[key] is not None]
        totals[key] = round(sum(float(value) for value in values), 2) if values else None
    return totals


def build_monthly_comparison(conn: sqlite3.Connection, *, asset_id: int, model_id: int) -> list[dict[str, Any]]:
    rows = list_model_monthly(conn, model_id=model_id)
    result: list[dict[str, Any]] = []
    for row in rows:
        month_start = date(int(row["base_year"]), int(row["month"]), 1)
        real = real_monthly_values(conn, asset_id=asset_id, month_start=month_start)
        expected_production = row["expected_production_kwh"]
        actual_production = real.get("actual_production_kwh")
        deviation = None
        deviation_pct = None
        if expected_production is not None and actual_production is not None:
            deviation = actual_production - float(expected_production)
            deviation_pct = deviation / float(expected_production) * 100 if float(expected_production) else None
        result.append(
            {
                "month": row["month"],
                "expected_production_kwh": expected_production,
                "actual_production_kwh": actual_production,
                "deviation_kwh": deviation,
                "deviation_pct": deviation_pct,
                "expected_consumption_kwh": row["expected_consumption_kwh"],
                "consumption_kwh": real.get("consumption_kwh"),
                "expected_self_use_kwh": row["expected_self_use_kwh"],
                "self_use_kwh": real.get("self_use_kwh"),
                "expected_export_kwh": row["expected_export_kwh"],
                "export_kwh": real.get("export_kwh"),
                "expected_grid_import_kwh": row["expected_grid_import_kwh"],
                "grid_import_kwh": real.get("grid_import_kwh"),
                "real_source": real.get("source"),
                "data_quality": real.get("data_quality"),
            }
        )
    return result


def real_monthly_values(conn: sqlite3.Connection, *, asset_id: int, month_start: date) -> dict[str, Any]:
    production_columns = {row["name"] for row in conn.execute("PRAGMA table_info(production_records)").fetchall()}
    month_text = month_start.isoformat()
    monthly = conn.execute(
        "SELECT * FROM production_records WHERE asset_id = ? AND period_type = 'month' AND period_date = ? ORDER BY id DESC LIMIT 1",
        (asset_id, month_text),
    ).fetchone()
    if monthly:
        return {
            "actual_production_kwh": _float_or_none(monthly["production_kwh"]),
            "consumption_kwh": _row_float(monthly, "consumption_kwh"),
            "self_use_kwh": _row_float(monthly, "self_use_kwh"),
            "export_kwh": _row_float(monthly, "export_kwh"),
            "grid_import_kwh": _row_float(monthly, "grid_import_kwh"),
            "source": "month",
            "data_quality": monthly["data_quality"] if "data_quality" in monthly.keys() else "",
        }
    start = month_start.isoformat()
    end_month = date(month_start.year + (1 if month_start.month == 12 else 0), 1 if month_start.month == 12 else month_start.month + 1, 1).isoformat()
    selectable = ["SUM(production_kwh) AS production_kwh"]
    for key in ("consumption_kwh", "self_use_kwh", "export_kwh", "grid_import_kwh"):
        selectable.append(f"SUM({key}) AS {key}" if key in production_columns else f"NULL AS {key}")
    selectable.append("COUNT(*) AS count")
    daily = conn.execute(
        f"""
        SELECT {", ".join(selectable)}
        FROM production_records
        WHERE asset_id = ? AND period_type = 'day' AND period_date >= ? AND period_date < ?
        """,
        (asset_id, start, end_month),
    ).fetchone()
    if daily and int(daily["count"] or 0) > 0:
        return {
            "actual_production_kwh": _row_float(daily, "production_kwh"),
            "consumption_kwh": _row_float(daily, "consumption_kwh"),
            "self_use_kwh": _row_float(daily, "self_use_kwh"),
            "export_kwh": _row_float(daily, "export_kwh"),
            "grid_import_kwh": _row_float(daily, "grid_import_kwh"),
            "source": "daily_sum",
            "data_quality": "",
        }
    return {"source": "none", "data_quality": "Sem dados"}


def _row_float(row: sqlite3.Row, key: str) -> float | None:
    if key not in row.keys() or row[key] is None:
        return None
    return _float_or_none(row[key])


def build_chart_series(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = ("production", "consumption", "self_use", "export", "grid_import")
    result = {"labels": [row["month"] for row in rows], "series": {}}
    for metric in metrics:
        expected_key = f"expected_{metric}_kwh"
        actual_key = "actual_production_kwh" if metric == "production" else f"{metric}_kwh"
        result["series"][metric] = {
            "expected": [row.get(expected_key) for row in rows],
            "actual": [row.get(actual_key) for row in rows],
        }
    return result


def compare_financial_models(conn: sqlite3.Connection, *, asset_id: int, left_id: int, right_id: int) -> dict[str, Any]:
    left = get_asset_model(conn, asset_id=asset_id, model_id=left_id)
    right = get_asset_model(conn, asset_id=asset_id, model_id=right_id)
    if left is None or right is None:
        raise FinancialModelError("financial_model_not_found")
    left_rows = {int(row["month"]): row for row in list_model_monthly(conn, model_id=left_id)}
    right_rows = {int(row["month"]): row for row in list_model_monthly(conn, model_id=right_id)}
    rows = []
    for month in range(1, 13):
        left_value = left_rows.get(month)["expected_production_kwh"] if month in left_rows else None
        right_value = right_rows.get(month)["expected_production_kwh"] if month in right_rows else None
        rows.append({"month": month, "left": left_value, "right": right_value, "delta": (float(right_value) - float(left_value)) if left_value is not None and right_value is not None else None})
    return {"left": left, "right": right, "rows": rows}


def get_active_expected_for_month(conn: sqlite3.Connection, *, asset_id: int | None, year: int, month: int) -> sqlite3.Row | None:
    return active_model_for_month(conn, asset_id=asset_id, year=year, month=month)


def parse_model_details_json(model: sqlite3.Row | None) -> dict[str, Any]:
    if model is None or "details_json" not in model.keys():
        return {}
    try:
        return json.loads(model["details_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
