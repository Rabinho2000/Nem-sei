"""The top-level Reporting area: individual reports and portfolio runs, in one
place, generated and downloaded from the browser instead of a Python shell.

Every render here goes through the same functions the golden tests pin against
V1: `assemble_asset_report`, `snapshot_dataset`, `build_customer_report_pdf`,
`build_asset_report_workbook`. This module supplies dates, files and HTTP
plumbing; it computes nothing.
"""
from __future__ import annotations

import io

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.reporting.assembler import assemble_asset_report, excel_payload_from_report
from nemsei.reporting.customer_pdf import build_customer_report_pdf
from nemsei.reporting.datasets import rehydrate_snapshot_payload, snapshot_dataset
from nemsei.reporting.excel import build_asset_report_workbook
from nemsei.reporting.models import ReportingDataset, ReportSnapshot
from nemsei.reporting.periods import ReportingPeriodError, monthly_period
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.reporting_queries import asset_report_history, portfolio_runs_overview, reports_overview, searchable_assets


reporting_bp = Blueprint("reporting", __name__, url_prefix="/reports")


def _actor() -> str:
    return (browser_session.get("username") or "").strip() or "operador"


@reporting_bp.get("")
@require_authenticated
def index() -> str:
    session = get_request_session()
    return render_template(
        "reporting/index.html",
        title="Relatórios",
        **reports_overview(session),
    )


@reporting_bp.get("/assets")
@require_authenticated
def assets_index() -> str:
    session = get_request_session()
    search = request.args.get("search", "").strip()
    return render_template(
        "reporting/assets.html",
        title="Relatórios por instalação",
        search=search,
        assets=searchable_assets(session, search=search),
    )


@reporting_bp.get("/assets/<int:asset_id>")
@require_authenticated
def asset_history(asset_id: int) -> str:
    session = get_request_session()
    context = asset_report_history(session, asset_id=asset_id)
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
    try:
        period = monthly_period(report_month)
        with session.begin():
            assembled = assemble_asset_report(session, asset_id=asset_id, period=period, built_by=_actor())
            snapshot_dataset(session, dataset=assembled.dataset, payload=assembled.payload, created_by=_actor())
    except ReportingPeriodError:
        flash("Mês inválido.", "error")
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash(f"Relatório de {period.label} gerado.", "success")
    return redirect(url_for("reporting.asset_history", asset_id=asset_id))


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
        runs=portfolio_runs_overview(session),
    )
