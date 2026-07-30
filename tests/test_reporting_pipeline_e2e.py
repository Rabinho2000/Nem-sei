from __future__ import annotations

from datetime import date

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.portfolio_repository import create_portfolio
from monitoring_board.report_template_repository import (
    get_default_template,
    save_report_automation,
)
from monitoring_board.reporting.distribution import create_distribution, create_recipient
from monitoring_board.reporting.monthly_close import build_asset_close_payload
from monitoring_board.reporting.quality_gate import BLOCKED, evaluate_report_quality
from monitoring_board.reporting.snapshots import (
    approve_snapshot,
    create_snapshot,
    get_snapshot,
    validate_snapshot,
)
from monitoring_board.reporting.templates import template_to_config


def test_approved_reporting_pipeline_is_frozen_and_audited(tmp_path) -> None:
    db_path = tmp_path / "pipeline.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        customer_id = int(
            conn.execute(
                """INSERT INTO customers
                   (name, nif, normalized_nif, active, created_at, updated_at)
                   VALUES ('Cliente Sintético', '500000001', '500000001', 1, '2026-01-01', '2026-01-01')"""
            ).lastrowid
        )
        asset_ids = [
            int(
                conn.execute(
                    "INSERT INTO assets (project_name, customer_id, nif, kwp) VALUES (?, ?, '500000001', '10')",
                    (name, customer_id),
                ).lastrowid
            )
            for name in ("Instalação Norte", "Instalação Sul")
        ]
        for asset_id in asset_ids:
            conn.execute(
                """INSERT INTO asset_integrations
                   (asset_id, provider, external_id, external_name, enabled)
                   VALUES (?, 'FusionSolar', ?, ?, 1)""",
                (asset_id, f"SYS-{asset_id}", f"Central {asset_id}"),
            )
        portfolio_id = create_portfolio(conn, name="Portefólio Sintético")
        for asset_id in asset_ids:
            conn.execute(
                """INSERT INTO portfolio_assets
                   (portfolio_id, asset_id, active, mapping_status, mapping_confidence, mapping_method)
                   VALUES (?, ?, 1, 'manual', 1, 'manual')""",
                (portfolio_id, asset_id),
            )
        asset_id = asset_ids[0]
        conn.execute(
            """INSERT INTO production_records
               (asset_id, provider, external_id, period_type, period_date,
                production_kwh, data_quality, created_at, updated_at)
               VALUES (?, 'FusionSolar', 'SYS-1', 'month', '2026-06-01',
                       300, 'ok', '2026-07-01', '2026-07-01')""",
            (asset_id,),
        )
        source_id = int(
            conn.execute(
                """INSERT INTO source_files
                   (asset_id, portfolio_id, file_type, original_filename, stored_path, uploaded_at, notes)
                   VALUES (?, ?, 'helioscope', 'synthetic.csv', 'fixture://synthetic', '2026-01-01', 'fixture')""",
                (asset_id, portfolio_id),
            ).lastrowid
        )
        conn.execute(
            """INSERT INTO helioscope_expected_production
               (asset_id, source_file_id, base_year, month, expected_kwh, imported_at)
               VALUES (?, ?, 2026, 6, 310, '2026-01-01')""",
            (asset_id, source_id),
        )
        payload = build_asset_close_payload(
            conn,
            asset_id=asset_id,
            report_month="2026-06",
            reference_date=date(2026, 7, 5),
        )
        template = get_default_template(conn, "individual")
        snapshot_id = create_snapshot(
            conn,
            scope_type="individual",
            asset_id=asset_id,
            period_type="monthly",
            period_start="2026-06-01",
            period_end="2026-06-30",
            payload=payload,
            template_id=template.id,
            template_version=1,
            template_snapshot=template_to_config(template),
            billing_snapshot={"billing_config_id": payload["billing_config_id"]},
            engine_version="e2e-test",
            created_by="tester",
        )
        quality = evaluate_report_quality(payload, scope="individual")
        validate_snapshot(conn, snapshot_id, quality, actor="validator")
        approve_snapshot(conn, snapshot_id, actor="approver")
        conn.execute(
            "UPDATE production_records SET production_kwh = 999 WHERE asset_id = ?",
            (asset_id,),
        )
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Pipeline E2E",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=2,
            schedule_time="09:00",
            formats=["pdf"],
            include_availability=0,
        )
        conn.commit()
        run = app_module.run_report_automation_generation(
            conn, automation_id, reference_date=date(2026, 7, 5)
        )
        generated = conn.execute(
            "SELECT * FROM report_generated_files WHERE run_id = ?", (run["run_id"],)
        ).fetchone()
        recipient_id = create_recipient(
            conn,
            name="Equipa Teste",
            email="reports@example.test",
            customer_id=customer_id,
        )
        distribution_id = create_distribution(
            conn,
            generated_file_id=int(generated["id"]),
            recipient_id=recipient_id,
            actor="tester",
        )
        conn.commit()

        assert get_snapshot(conn, snapshot_id).payload["production_kwh"] == 300
        assert generated["snapshot_id"] == snapshot_id
        assert generated["sha256"]
        assert conn.execute(
            "SELECT status FROM report_distributions WHERE id = ?", (distribution_id,)
        ).fetchone()["status"] == "ready_to_send"
        assert conn.execute(
            "SELECT COUNT(*) FROM report_snapshot_events WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()[0] >= 3


def test_blocked_pipeline_creates_no_distributable_file(tmp_path) -> None:
    db_path = tmp_path / "blocked-pipeline.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        asset_id = int(
            conn.execute("INSERT INTO assets (project_name) VALUES ('Central Parcial')").lastrowid
        )
        template = get_default_template(conn, "individual")
        payload = {
            "asset_id": asset_id,
            "energy_provider": "FusionSolar",
            "production_quality_status": "partial",
        }
        snapshot_id = create_snapshot(
            conn,
            scope_type="individual",
            asset_id=asset_id,
            period_type="monthly",
            period_start="2026-06-01",
            period_end="2026-06-30",
            payload=payload,
            template_id=template.id,
            template_version=1,
            template_snapshot=template_to_config(template),
            engine_version="e2e-test",
        )
        quality = evaluate_report_quality(payload, scope="individual")
        assert quality.status == BLOCKED
        assert validate_snapshot(conn, snapshot_id, quality) == "blocked"
        automation_id = save_report_automation(
            conn,
            automation_id=None,
            name="Pipeline bloqueada",
            active=1,
            report_type="individual",
            asset_id=asset_id,
            portfolio_id=None,
            template_id=int(template.id),
            profile_id=None,
            schedule_day=2,
            schedule_time="09:00",
            formats=["pdf"],
            include_availability=0,
        )
        conn.commit()
        run = app_module.run_report_automation_generation(
            conn, automation_id, reference_date=date(2026, 7, 5)
        )
        assert run["status"] == "blocked"
        assert conn.execute("SELECT COUNT(*) FROM report_generated_files").fetchone()[0] == 0
