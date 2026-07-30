from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from monitoring_board.portfolio_report_repository import (
    get_default_profile,
    latest_profile_version,
)
from monitoring_board.reporting.monthly_close import (
    build_asset_close_payload,
    evaluate_close_payload,
)
from monitoring_board.reporting.distribution import (
    create_distribution,
    create_recipient,
    transition_distribution,
)
from monitoring_board.reporting.periods import month_bounds
from monitoring_board.reporting.portfolio import profile_to_config, result_to_dict
from monitoring_board.reporting.snapshots import (
    approve_snapshot,
    create_snapshot,
    get_snapshot,
    list_snapshots,
    reject_snapshot,
    validate_snapshot,
)
from monitoring_board.reporting.templates import template_to_config
from monitoring_board.report_template_repository import (
    get_default_template,
    latest_template_version,
)
from monitoring_board.services.portfolio_reporting import prepare_portfolio_report


reporting_bp = Blueprint("reporting", __name__, url_prefix="/reporting")


@reporting_bp.route("/monthly-close")
def monthly_close():
    report_month = normalize_month(request.args.get("report_month", ""))
    scope_type = request.args.get("scope_type", "individual")
    assets = g.db.execute(
        """
        SELECT a.id, a.project_name, c.name AS customer_name
        FROM assets a
        LEFT JOIN customers c ON c.id = a.customer_id
        ORDER BY c.name COLLATE NOCASE, a.project_name COLLATE NOCASE
        """
    ).fetchall()
    portfolios = g.db.execute(
        "SELECT id, name FROM portfolio_groups WHERE active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    snapshots = list_snapshots(
        g.db,
        scope_type=scope_type if scope_type in {"individual", "portfolio"} else None,
        period_start=f"{report_month}-01",
    )
    return render_template(
        "reporting/monthly_close.html",
        report_month=report_month,
        scope_type=scope_type,
        assets=assets,
        portfolios=portfolios,
        snapshots=snapshots,
    )


@reporting_bp.route("/monthly-close/snapshot", methods=["POST"])
def create_monthly_close_snapshot():
    report_month = normalize_month(request.form.get("report_month", ""))
    scope_type = request.form.get("scope_type", "individual")
    start, end = month_bounds(report_month)
    actor = str(session.get("username") or "")
    template = get_default_template(g.db, "portfolio" if scope_type == "portfolio" else "individual")
    if template is None:
        flash("Não existe um template predefinido para este relatório.", "error")
        return redirect(url_for("reporting.monthly_close", report_month=report_month, scope_type=scope_type))
    template_version = latest_template_version(g.db, template.id)
    if scope_type == "individual":
        asset_id = int(request.form.get("asset_id", "0") or 0)
        payload = build_asset_close_payload(
            g.db,
            asset_id=asset_id,
            report_month=report_month,
            reference_date=date.today(),
        )
        snapshot_id = create_snapshot(
            g.db,
            scope_type="individual",
            asset_id=asset_id,
            period_type="monthly",
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            payload=payload,
            template_id=template.id,
            template_version=template_version,
            template_snapshot=template_to_config(template),
            billing_snapshot={"billing_config_id": payload.get("billing_config_id")},
            energy_sources=[{
                "provider": payload.get("energy_provider"),
                "source": payload.get("production_source"),
            }],
            source_versions={"production_period": start.isoformat()},
            coverage={
                "available_days": payload.get("production_available_days"),
                "expected_days": payload.get("production_expected_days"),
            },
            engine_version="monthly-close-v1",
            created_by=actor,
        )
    elif scope_type == "portfolio":
        portfolio_id = int(request.form.get("portfolio_id", "0") or 0)
        portfolio = g.db.execute(
            "SELECT * FROM portfolio_groups WHERE id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise ValueError("portfolio_not_found")
        profile = get_default_profile(g.db, portfolio_id)
        profile_version = latest_profile_version(g.db, profile.id)
        result = prepare_portfolio_report(
            g.db,
            portfolio_id=portfolio_id,
            portfolio_name=portfolio["name"],
            profile=profile,
            report_month=report_month,
            profile_version=profile_version,
            reference_date=date.today(),
        )
        payload = result_to_dict(result)
        snapshot_id = create_snapshot(
            g.db,
            scope_type="portfolio",
            portfolio_id=portfolio_id,
            period_type="monthly",
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            payload=payload,
            profile_id=profile.id,
            profile_version=profile_version,
            profile_snapshot=profile_to_config(profile),
            template_id=template.id,
            template_version=template_version,
            template_snapshot=template_to_config(template),
            energy_sources=[{"provider": "SQLite", "source": "portfolio_reporting"}],
            source_versions={"engine_version": result.engine_version},
            coverage={
                "global_pct": str(result.coverage.global_pct),
                "by_source": {
                    key: str(value) for key, value in result.coverage.by_source.items()
                },
                "complete_installations": result.coverage.complete_installations,
                "incomplete_installations": result.coverage.incomplete_installations,
                "missing_months": list(result.coverage.missing_months),
            },
            engine_version=result.engine_version,
            created_by=actor,
        )
    else:
        raise ValueError("invalid_snapshot_scope")
    g.db.commit()
    flash(f"Snapshot #{snapshot_id} criado como rascunho.", "success")
    return redirect(url_for("reporting.snapshot_preview", snapshot_id=snapshot_id))


@reporting_bp.route("/snapshots/<int:snapshot_id>/validate", methods=["POST"])
def validate_monthly_snapshot(snapshot_id: int):
    snapshot = get_snapshot(g.db, snapshot_id)
    if snapshot is None:
        raise ValueError("snapshot_not_found")
    template_config = snapshot.template_snapshot
    metrics = set(template_config.get("metrics") or ())
    sections = {
        str(item.get("key"))
        for item in template_config.get("sections") or ()
        if isinstance(item, dict) and item.get("enabled", True)
    }
    quality = evaluate_close_payload(
        snapshot.payload,
        scope=snapshot.scope_type,
        requires_financials=bool(metrics & {"financial", "net_benefit_eur", "estimated_value_eur"}) or "financial" in sections,
        requires_availability="availability" in sections,
        requires_customer=snapshot.scope_type == "portfolio",
        requires_expected_production=bool(
            metrics
            & {
                "expected_production_kwh",
                "adjusted_expected_kwh",
                "expected_specific_yield",
                "deviation_kwh",
                "deviation_pct",
                "performance_vs_expected_pct",
                "performance_ratio",
            }
        )
        or bool(sections & {"performance", "expected_production", "financial"}),
    )
    status = validate_snapshot(
        g.db,
        snapshot_id,
        quality,
        actor=str(session.get("username") or ""),
    )
    g.db.commit()
    flash(
        "Snapshot validado." if status == "validated" else "Validação bloqueada; consulta os findings.",
        "success" if status == "validated" else "error",
    )
    return redirect(url_for("reporting.snapshot_preview", snapshot_id=snapshot_id))


@reporting_bp.route("/snapshots/<int:snapshot_id>/approve", methods=["POST"])
def approve_monthly_snapshot(snapshot_id: int):
    try:
        approve_snapshot(
            g.db, snapshot_id, actor=str(session.get("username") or "")
        )
        g.db.commit()
    except ValueError:
        g.db.rollback()
        flash("A aprovação está bloqueada pelos findings de qualidade.", "error")
    else:
        flash("Snapshot aprovado e congelado.", "success")
    return redirect(url_for("reporting.snapshot_preview", snapshot_id=snapshot_id))


@reporting_bp.route("/snapshots/<int:snapshot_id>/reject", methods=["POST"])
def reject_monthly_snapshot(snapshot_id: int):
    try:
        reject_snapshot(
            g.db,
            snapshot_id,
            actor=str(session.get("username") or ""),
            reason=request.form.get("reason", ""),
        )
        g.db.commit()
    except ValueError:
        g.db.rollback()
        flash("Indica o motivo da rejeição.", "error")
    else:
        flash("Snapshot rejeitado.", "success")
    return redirect(url_for("reporting.snapshot_preview", snapshot_id=snapshot_id))


@reporting_bp.route("/snapshots/<int:snapshot_id>")
def snapshot_preview(snapshot_id: int):
    snapshot = get_snapshot(g.db, snapshot_id)
    if snapshot is None:
        return redirect(url_for("reporting.monthly_close"))
    events = g.db.execute(
        """
        SELECT * FROM report_snapshot_events
        WHERE snapshot_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (snapshot_id,),
    ).fetchall()
    return render_template(
        "reporting/snapshot_preview.html",
        snapshot=snapshot,
        events=events,
    )


@reporting_bp.route("/distribution")
def distribution_queue():
    recipients = g.db.execute(
        """
        SELECT r.*, c.name AS customer_name, a.project_name AS asset_name,
               p.name AS portfolio_name
        FROM report_recipients r
        LEFT JOIN customers c ON c.id = r.customer_id
        LEFT JOIN assets a ON a.id = r.asset_id
        LEFT JOIN portfolio_groups p ON p.id = r.portfolio_id
        ORDER BY r.active DESC, r.name COLLATE NOCASE
        """
    ).fetchall()
    distributions = g.db.execute(
        """
        SELECT d.*, r.name AS recipient_name, f.filename, f.id AS file_id
        FROM report_distributions d
        JOIN report_recipients r ON r.id = d.recipient_id
        JOIN report_generated_files f ON f.id = d.generated_file_id
        ORDER BY d.created_at DESC, d.id DESC
        """
    ).fetchall()
    files = g.db.execute(
        """
        SELECT f.* FROM report_generated_files f
        JOIN report_snapshots s ON s.id = f.snapshot_id
        WHERE f.status = 'completed' AND s.approval_status = 'approved'
        ORDER BY f.created_at DESC
        """
    ).fetchall()
    return render_template(
        "reporting/distribution.html",
        recipients=recipients,
        distributions=distributions,
        files=files,
        customers=g.db.execute("SELECT id, name FROM customers WHERE active = 1 ORDER BY name").fetchall(),
        assets=g.db.execute("SELECT id, project_name FROM assets ORDER BY project_name").fetchall(),
        portfolios=g.db.execute("SELECT id, name FROM portfolio_groups WHERE active = 1 ORDER BY name").fetchall(),
    )


@reporting_bp.post("/distribution/recipients")
def add_distribution_recipient():
    scope_type = request.form.get("scope_type", "")
    scope_id = int(request.form.get("scope_id", "0") or 0)
    try:
        create_recipient(
            g.db,
            name=request.form.get("name", ""),
            email=request.form.get("email", ""),
            customer_id=scope_id if scope_type == "customer" else None,
            asset_id=scope_id if scope_type == "asset" else None,
            portfolio_id=scope_id if scope_type == "portfolio" else None,
        )
        g.db.commit()
        flash("Destinatário criado.", "success")
    except (ValueError, sqlite3.IntegrityError):
        g.db.rollback()
        flash("Destinatário inválido ou já existente para este âmbito.", "error")
    return redirect(url_for("reporting.distribution_queue"))


@reporting_bp.post("/distribution/items")
def add_distribution_item():
    try:
        create_distribution(
            g.db,
            generated_file_id=int(request.form.get("generated_file_id", "0") or 0),
            recipient_id=int(request.form.get("recipient_id", "0") or 0),
            actor=str(session.get("username") or ""),
        )
        g.db.commit()
        flash("Ficheiro preparado para distribuição manual.", "success")
    except ValueError:
        g.db.rollback()
        flash("O ficheiro ou destinatário não cumpre os requisitos de distribuição.", "error")
    return redirect(url_for("reporting.distribution_queue"))


@reporting_bp.post("/distribution/items/<int:distribution_id>/<action>")
def update_distribution_item(distribution_id: int, action: str):
    targets = {
        "approve": "approved_to_send",
        "cancel": "cancelled",
        "retry": "ready_to_send",
    }
    if action not in targets:
        raise ValueError("invalid_distribution_action")
    try:
        transition_distribution(
            g.db,
            distribution_id,
            targets[action],
            actor=str(session.get("username") or ""),
        )
        g.db.commit()
        flash("Estado de distribuição atualizado.", "success")
    except ValueError:
        g.db.rollback()
        flash("A transição de estado não é permitida.", "error")
    return redirect(url_for("reporting.distribution_queue"))


def normalize_month(value: str) -> str:
    try:
        return date.fromisoformat(f"{value[:7]}-01").strftime("%Y-%m")
    except ValueError:
        previous = date.today().replace(day=1)
        return (previous - timedelta(days=1)).strftime("%Y-%m")
