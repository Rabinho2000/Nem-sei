"""The Reporting area: a workspace for closing a month, not a list of files.

Every render goes through the same functions the golden tests pin against V1 --
`assemble_asset_report`, `snapshot_dataset`, `build_customer_report_pdf`,
`build_asset_report_workbook`. This module supplies dates, filters, files and
HTTP plumbing; it computes nothing, and it can reach no provider. A renderer
that called an API would make a frozen report unreproducible, which is the one
thing snapshots exist to prevent.
"""
from __future__ import annotations

import io

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.reporting.assembler import assemble_asset_report, excel_payload_from_report
from nemsei.reporting.customer_pdf import build_customer_report_pdf
from nemsei.reporting.datasets import rehydrate_snapshot_payload, snapshot_dataset
from nemsei.reporting.excel import build_asset_report_workbook
from nemsei.reporting.models import ReportingDataset, ReportSnapshot
from nemsei.reporting.periods import ReportingPeriodError, monthly_period, normalize_report_month
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.reporting_queries import (
    asset_report_history,
    default_month,
    portfolio_runs_overview,
    readiness_index,
    workspace_overview,
)


reporting_bp = Blueprint("reporting", __name__, url_prefix="/reports")


def _actor() -> str:
    return (browser_session.get("username") or "").strip() or "operador"


def _month() -> str:
    """The month being worked on, from the query string or the calendar."""
    return normalize_report_month(request.args.get("month"), today=None) if request.args.get("month") else default_month()


@reporting_bp.get("")
@require_authenticated
def index() -> str:
    session = get_request_session()
    return render_template(
        "reporting/index.html",
        title="Relatórios",
        **workspace_overview(session, month=_month()),
    )


@reporting_bp.get("/assets")
@require_authenticated
def assets_index() -> str:
    session = get_request_session()
    return render_template(
        "reporting/assets.html",
        title="Relatórios por instalação",
        **readiness_index(
            session,
            month=_month(),
            contract=request.args.get("contract", "").strip(),
            state=request.args.get("state", "").strip(),
            generated=request.args.get("generated", "").strip(),
            search=request.args.get("search", "").strip(),
        ),
    )


@reporting_bp.get("/assets/<int:asset_id>")
@require_authenticated
def asset_history(asset_id: int) -> str:
    session = get_request_session()
    context = asset_report_history(session, asset_id=asset_id, month=_month())
    if context is None:
        abort(404)
    return render_template(
        "reporting/asset_history.html",
        title=f"Relatórios — {context['asset'].canonical_name}",
        csrf_token=token(),
        **context,
    )


@reporting_bp.post("/assets/<int:asset_id>/generate")
@require_authenticated
def generate_asset_report(asset_id: int):
    require_valid_token()
    session = get_request_session()
    report_month = request.form.get("month", "").strip()
    period = None
    try:
        period = monthly_period(report_month)
        with session.begin():
            assembled = assemble_asset_report(session, asset_id=asset_id, period=period, built_by=_actor())
            snapshot_dataset(session, dataset=assembled.dataset, payload=assembled.payload, created_by=_actor())
            state = assembled.payload.get("reporting_state")
    except ReportingPeriodError:
        flash("Mês inválido.", "error")
    except ValueError as error:
        flash(str(error), "error")
    else:
        # Never "gerado" alone. A report that is provisional has to say so at
        # the moment it is produced, or the operator learns it only by opening
        # the document -- and a provisional report states no euros at all.
        if state == "final":
            flash(f"Relatório final de {period.label} gerado.", "success")
        elif state == "blocked":
            flash(f"Sem dados de energia para {period.label}: não há relatório a produzir.", "error")
        else:
            flash(f"Relatório PROVISÓRIO de {period.label} gerado. Sem valores em euros até o mês fechar.", "warning")
    return redirect(url_for("reporting.asset_history", asset_id=asset_id, month=report_month or None))


def _load_snapshot(session, asset_id: int, snapshot_id: int) -> ReportSnapshot:
    snapshot = session.get(ReportSnapshot, snapshot_id)
    dataset = session.get(ReportingDataset, snapshot.dataset_id) if snapshot else None
    if snapshot is None or dataset is None or dataset.asset_id != asset_id:
        abort(404)
    return snapshot


@reporting_bp.get("/assets/<int:asset_id>/snapshots/<int:snapshot_id>.pdf")
@require_authenticated
def asset_snapshot_pdf(asset_id: int, snapshot_id: int):
    session = get_request_session()
    snapshot = _load_snapshot(session, asset_id, snapshot_id)
    payload = rehydrate_snapshot_payload(snapshot.payload_json)
    pdf_bytes = build_customer_report_pdf(payload)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="relatorio-{asset_id}-{snapshot_id}.pdf"'},
    )


@reporting_bp.get("/assets/<int:asset_id>/snapshots/<int:snapshot_id>.xlsx")
@require_authenticated
def asset_snapshot_xlsx(asset_id: int, snapshot_id: int):
    session = get_request_session()
    snapshot = _load_snapshot(session, asset_id, snapshot_id)
    payload = rehydrate_snapshot_payload(snapshot.payload_json)
    workbook = build_asset_report_workbook(excel_payload_from_report(payload))
    stream = io.BytesIO()
    workbook.save(stream)
    return Response(
        stream.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="relatorio-{asset_id}-{snapshot_id}.xlsx"'},
    )


@reporting_bp.get("/portfolios")
@require_authenticated
def portfolios_index() -> str:
    session = get_request_session()
    return render_template(
        "reporting/portfolios.html",
        title="Relatórios de portfolio",
        month=_month(),
        runs=portfolio_runs_overview(session),
    )
