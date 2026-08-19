"""Portfolio screens. Routes stay thin; the reads live in `portfolio_queries`."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from nemsei.portfolios.datasets import build_portfolio_dataset
from nemsei.portfolios.service import (
    add_rule,
    create_portfolio,
    end_membership,
    freeze_snapshot,
    resolve_member_to_asset,
)
from nemsei.reporting.periods import ReportingPeriodError, exclusive_end, monthly_period
from nemsei.web.csrf import require_valid_token, token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.portfolio_queries import SECTIONS, default_month, portfolio_detail, portfolio_list


portfolio_bp = Blueprint("portfolios", __name__)

SECTION_KEYS = {key for key, _ in SECTIONS}


@portfolio_bp.get("/portfolios")
@require_authenticated
def index() -> str:
    return render_template(
        "portfolios/list.html",
        title="Portfolios",
        csrf_token=token(),
        **portfolio_list(get_request_session()),
    )


@portfolio_bp.post("/portfolios")
@require_authenticated
def create():
    require_valid_token()
    session = get_request_session()
    name = request.form.get("name", "").strip()
    try:
        with session.begin():
            portfolio = create_portfolio(
                session,
                name=name,
                description=request.form.get("description", "").strip() or None,
                created_by=request.form.get("actor", "operador").strip() or "operador",
            )
            portfolio_id = portfolio.id
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("portfolios.index"))
    flash(f"Portfolio “{name}” criado.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id))


@portfolio_bp.get("/portfolios/<int:portfolio_id>")
@portfolio_bp.get("/portfolios/<int:portfolio_id>/<section>")
@require_authenticated
def detail(portfolio_id: int, section: str = "overview") -> str:
    if section not in SECTION_KEYS:
        abort(404)
    session = get_request_session()
    report_month = request.args.get("month", "").strip() or default_month(session, portfolio_id)
    try:
        monthly_period(report_month)
    except ReportingPeriodError:
        report_month = date.today().strftime("%Y-%m")
    filters = {
        key: request.args.get(key, "").strip()
        for key in ("country_code", "lifecycle_status", "provider_code", "contract_type", "resolution_state", "q", "attention")
    }
    try:
        context = portfolio_detail(
            session, portfolio_id=portfolio_id, section=section, report_month=report_month, filters=filters
        )
    except ValueError:
        abort(404)
    return render_template(
        f"portfolios/{section}.html",
        title=context["portfolio"].name,
        csrf_token=token(),
        **context,
    )


@portfolio_bp.post("/portfolios/<int:portfolio_id>/build")
@require_authenticated
def build(portfolio_id: int):
    """Freeze the period's membership and aggregate it. This is the batch unit."""
    require_valid_token()
    session = get_request_session()
    report_month = request.form.get("month", "").strip()
    actor = request.form.get("actor", "operador").strip() or "operador"
    try:
        period = monthly_period(report_month)
    except ReportingPeriodError:
        flash("Mês inválido.", "error")
        return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id))
    try:
        with session.begin():
            snapshot = freeze_snapshot(
                session,
                portfolio_id=portfolio_id,
                period_start=period.start,
                # The period end is inclusive; the snapshot stores it exclusive.
                period_end=exclusive_end(period),
                created_by=actor,
            )
            build_portfolio_dataset(session, snapshot=snapshot, built_by=actor)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id))
    flash(f"Dados de {period.label} construídos a partir dos relatórios individuais.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, month=report_month))


@portfolio_bp.post("/portfolios/<int:portfolio_id>/members/<int:membership_id>/resolve")
@require_authenticated
def resolve_member(portfolio_id: int, membership_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        asset_id = int(request.form.get("asset_id", "").strip())
    except ValueError:
        flash("Indica a instalação.", "error")
        return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="installations"))
    actor = request.form.get("actor", "operador").strip() or "operador"
    try:
        with session.begin():
            resolve_member_to_asset(session, membership_id=membership_id, asset_id=asset_id, resolved_by=actor)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Membro associado à instalação.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="installations"))


@portfolio_bp.post("/portfolios/<int:portfolio_id>/members/<int:membership_id>/end")
@require_authenticated
def end_member(portfolio_id: int, membership_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        on = date.fromisoformat(request.form.get("on", "").strip())
    except ValueError:
        flash("Data inválida.", "error")
        return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="settings"))
    try:
        with session.begin():
            end_membership(session, membership_id=membership_id, on=on)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Membro terminado. O histórico é preservado.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="settings"))


@portfolio_bp.post("/portfolios/<int:portfolio_id>/rules")
@require_authenticated
def create_rule(portfolio_id: int):
    require_valid_token()
    session = get_request_session()
    values = [value.strip() for value in request.form.get("values", "").split(",") if value.strip()]
    try:
        with session.begin():
            add_rule(
                session,
                portfolio_id=portfolio_id,
                attribute=request.form.get("attribute", "").strip(),
                operator=request.form.get("operator", "in").strip() or "in",
                values=values,
                created_by=request.form.get("actor", "operador").strip() or "operador",
            )
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Filtro aplicado ao portfolio.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="settings"))
