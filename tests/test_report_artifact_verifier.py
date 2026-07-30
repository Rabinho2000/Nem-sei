import hashlib
import io
from datetime import date, datetime, timedelta
from pathlib import Path

from reportlab.pdfgen import canvas

from app import ensure_database
from monitoring_board.db import get_db
from monitoring_board.report_artifact_verifier import verify_expertcom_report


def _pdf_payload() -> bytes:
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 760, "Expertcom")
    document.drawString(72, 740, "Junho 2026")
    document.drawString(72, 720, "3 000 kWh")
    document.showPage()
    document.save()
    return buffer.getvalue()


def test_expertcom_report_verifier_cross_checks_database_storage_and_pdf(
    tmp_path: Path,
) -> None:
    database = tmp_path / "monitoring_board.db"
    storage = tmp_path / "uploads" / "generated_reports"
    report_dir = storage / "1"
    report_dir.mkdir(parents=True)
    ensure_database(str(database))
    payload = _pdf_payload()
    report_path = report_dir / "Expertcom_Junho_2026.pdf"
    report_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    with get_db(str(database)) as conn:
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Expertcom')"
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO asset_integrations (
                asset_id, provider, external_id, external_name, enabled,
                is_primary_energy_source
            ) VALUES (?, 'Sigenergy', 'TZXRS1780315946', 'Expertcom', 1, 1)
            """,
            (asset_id,),
        )
        for offset in range(30):
            target = date(2026, 6, 1) + timedelta(days=offset)
            period_start = datetime.combine(target, datetime.min.time())
            period_end = period_start + timedelta(days=1)
            conn.execute(
                """
                INSERT INTO energy_interval_facts (
                    asset_id, provider, external_id, period_start, period_end,
                    granularity, production_kwh, provenance, data_quality,
                    created_at, updated_at
                ) VALUES (?, 'Sigenergy', 'TZXRS1780315946', ?, ?, 'day',
                          100, 'sigenergy_system_history', 'complete', ?, ?)
                """,
                (
                    asset_id,
                    period_start.isoformat(),
                    period_end.isoformat(),
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO production_records (
                    asset_id, provider, external_id, period_type, period_date,
                    production_kwh, data_quality, created_at, updated_at
                ) VALUES (?, 'Sigenergy', 'TZXRS1780315946', 'day', ?,
                          100, 'complete', ?, ?)
                """,
                (
                    asset_id,
                    target.isoformat(),
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            )
        conn.execute(
            """
            INSERT INTO production_records (
                asset_id, provider, external_id, period_type, period_date,
                production_kwh, data_quality, created_at, updated_at
            ) VALUES (?, 'Sigenergy', 'TZXRS1780315946', 'month',
                      '2026-06-01', 3000, 'complete', ?, ?)
            """,
            (asset_id, datetime.now().isoformat(), datetime.now().isoformat()),
        )
        run_id = int(
            conn.execute(
                """
                INSERT INTO report_generation_runs (
                    report_type, asset_id, period_type, period_start,
                    period_end, status, requested_count, completed_count,
                    created_at, completed_at
                ) VALUES ('individual', ?, 'monthly', '2026-06-01',
                          '2026-06-30', 'completed', 1, 1, ?, ?)
                """,
                (
                    asset_id,
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            ).lastrowid
        )
        file_id = int(
            conn.execute(
                """
                INSERT INTO report_generated_files (
                    run_id, asset_id, format, filename, relative_path, sha256,
                    size_bytes, status, period_type, period_start, period_end,
                    is_auxiliary, created_at
                ) VALUES (?, ?, 'pdf', ?, ?, ?, ?, 'completed', 'monthly',
                          '2026-06-01', '2026-06-30', 0, ?)
                """,
                (
                    run_id,
                    asset_id,
                    report_path.name,
                    "1/Expertcom_Junho_2026.pdf",
                    digest,
                    len(payload),
                    datetime.now().isoformat(),
                ),
            ).lastrowid
        )
        conn.commit()

        result = verify_expertcom_report(
            conn,
            storage_root=storage,
            file_id=file_id,
        )

    assert result["status"] == "validated"
    assert result["production_kwh"] == 3000
    assert result["daily_records"] == 30
    assert result["energy_interval_facts"] == 30
    assert result["sha256"] == digest
    assert result["pdf_text_validated"] is True
