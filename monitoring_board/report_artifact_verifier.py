from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from monitoring_board.customer_reports import format_kwh
from monitoring_board.db import get_db
from monitoring_board.runtime import DB_PATH, UPLOAD_DIR, path_is_within


EXPERTCOM_SYSTEM_ID = "TZXRS1780315946"
EXPECTED_PERIOD_START = "2026-06-01"
EXPECTED_PERIOD_END = "2026-06-30"


def _resolve_report_path(relative_path: str, storage_root: Path) -> Path:
    stored = Path(relative_path)
    candidates = (
        (stored,)
        if stored.is_absolute()
        else (
            storage_root / stored,
            storage_root.parent / stored,
            storage_root.parent.parent / stored,
        )
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if path_is_within(resolved, storage_root):
            return resolved
    raise ValueError("O caminho persistido do relatório não pertence ao storage.")


def _extract_pdf_text(payload: bytes) -> tuple[int, str]:
    reader = PdfReader(io.BytesIO(payload))
    if not reader.pages:
        raise ValueError("O PDF não contém páginas.")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return len(reader.pages), " ".join(text.split())


def verify_expertcom_report(
    conn: sqlite3.Connection,
    *,
    storage_root: Path,
    file_id: int | None = None,
) -> dict[str, Any]:
    params: tuple[Any, ...]
    file_filter = ""
    if file_id is not None:
        file_filter = "AND generated.id = ?"
        params = (file_id,)
    else:
        params = ()
    row = conn.execute(
        f"""
        SELECT
            generated.*,
            run.report_type,
            run.status AS run_status,
            asset.project_name
        FROM report_generated_files generated
        JOIN report_generation_runs run ON run.id = generated.run_id
        JOIN assets asset ON asset.id = generated.asset_id
        WHERE generated.format = 'pdf'
          AND generated.status = 'completed'
          AND COALESCE(generated.is_auxiliary, 0) = 0
          AND generated.period_start = '{EXPECTED_PERIOD_START}'
          AND generated.period_end = '{EXPECTED_PERIOD_END}'
          AND LOWER(asset.project_name) = 'expertcom'
          {file_filter}
        ORDER BY generated.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise ValueError("Não existe PDF concluído da Expertcom para junho de 2026.")
    if row["report_type"] != "individual" or row["run_status"] != "completed":
        raise ValueError("O PDF não pertence a um run individual concluído.")

    mapping = conn.execute(
        """
        SELECT *
        FROM asset_integrations
        WHERE asset_id = ? AND provider = 'Sigenergy'
          AND external_id = ? AND enabled = 1
          AND is_primary_energy_source = 1
        """,
        (row["asset_id"], EXPERTCOM_SYSTEM_ID),
    ).fetchone()
    if mapping is None:
        raise ValueError("A fonte primária do relatório não é a Expertcom Sigenergy.")

    month = conn.execute(
        """
        SELECT *
        FROM production_records
        WHERE asset_id = ? AND provider = 'Sigenergy'
          AND external_id = ? AND period_type = 'month'
          AND period_date = ? AND data_quality = 'complete'
        """,
        (row["asset_id"], EXPERTCOM_SYSTEM_ID, EXPECTED_PERIOD_START),
    ).fetchone()
    if month is None or month["production_kwh"] is None:
        raise ValueError("O total mensal Sigenergy completo não está materializado.")
    production_total = float(month["production_kwh"])

    daily = conn.execute(
        """
        SELECT
            COUNT(DISTINCT period_date) AS day_count,
            SUM(production_kwh) AS production_total
        FROM production_records
        WHERE asset_id = ? AND provider = 'Sigenergy'
          AND external_id = ? AND period_type = 'day'
          AND period_date BETWEEN ? AND ?
          AND data_quality = 'complete'
        """,
        (
            row["asset_id"],
            EXPERTCOM_SYSTEM_ID,
            EXPECTED_PERIOD_START,
            EXPECTED_PERIOD_END,
        ),
    ).fetchone()
    if int(daily["day_count"] or 0) != 30:
        raise ValueError("A cobertura diária Sigenergy não contém os 30 dias.")
    if abs(float(daily["production_total"] or 0) - production_total) > 1e-6:
        raise ValueError("O total mensal diverge da soma dos dias persistidos.")

    fact_count = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT substr(period_start, 1, 10))
            FROM energy_interval_facts
            WHERE asset_id = ? AND provider = 'Sigenergy'
              AND external_id = ? AND granularity = 'day'
              AND substr(period_start, 1, 10) BETWEEN ? AND ?
              AND data_quality = 'complete'
            """,
            (
                row["asset_id"],
                EXPERTCOM_SYSTEM_ID,
                EXPECTED_PERIOD_START,
                EXPECTED_PERIOD_END,
            ),
        ).fetchone()[0]
        or 0
    )
    if fact_count != 30:
        raise ValueError("energy_interval_facts não contém os 30 dias completos.")

    path = _resolve_report_path(str(row["relative_path"]), storage_root)
    if path.is_symlink() or not path.is_file():
        raise ValueError("O ficheiro persistido é inválido.")
    payload = path.read_bytes()
    if not payload:
        raise ValueError("O PDF persistido está vazio.")
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) != int(row["size_bytes"] or 0):
        raise ValueError("O tamanho persistido não coincide com o ficheiro.")
    if digest != str(row["sha256"] or ""):
        raise ValueError("O SHA-256 persistido não coincide com o ficheiro.")

    page_count, text = _extract_pdf_text(payload)
    if "Expertcom" not in text:
        raise ValueError("O PDF não contém o nome Expertcom.")
    if "Junho 2026" not in text and "2026-06" not in text:
        raise ValueError("O PDF não contém o período de junho de 2026.")
    formatted_total = format_kwh(production_total)
    if formatted_total not in text:
        raise ValueError("O PDF não contém o total de produção persistido.")

    return {
        "status": "validated",
        "file_id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "asset_id": int(row["asset_id"]),
        "filename": str(row["filename"]),
        "path": str(path),
        "download_path": f"/report-generation/files/{int(row['id'])}",
        "size_bytes": len(payload),
        "sha256": digest,
        "period_start": EXPECTED_PERIOD_START,
        "period_end": EXPECTED_PERIOD_END,
        "production_kwh": production_total,
        "formatted_production": formatted_total,
        "daily_records": int(daily["day_count"]),
        "energy_interval_facts": fact_count,
        "pdf_pages": page_count,
        "pdf_text_validated": True,
        "storage_integrity": "valid",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Valida o relatório real da Expertcom sem alterar dados."
    )
    parser.add_argument("--file-id", type=int)
    args = parser.parse_args()
    with get_db(str(DB_PATH)) as conn:
        result = verify_expertcom_report(
            conn,
            storage_root=UPLOAD_DIR / "generated_reports",
            file_id=args.file_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
