"""Portfolio screens. Routes stay thin; the reads live in `portfolio_queries`."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, session as browser_session, url_for

from nemsei.portfolios.datasets import build_portfolio_dataset
from nemsei.portfolios.reporting import approve_run, generate_report_run, mark_run_reviewed
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
from nemsei.web.portfolio_queries import SECTIONS, default_month, member_review_data, portfolio_detail, portfolio_list


portfolio_bp = Blueprint("portfolios", __name__)

SECTION_KEYS = {key for key, _ in SECTIONS}


def _actor() -> str:
    """Who is acting, from the one identity the session actually has.

    V2 has no per-user accounts yet — a single administrator login, per
    `KNOWN_GAPS.md` — so this is the best attribution available. It is still
    real: an operator's session username, not a constant every action shared.
    """
    return (browser_session.get("username") or "").strip() or "operador"


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
                created_by=_actor(),
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
    actor = _actor()
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


def _member_redirect(portfolio_id: int):
    """Back to wherever a member action was started from: review or settings."""
    if request.form.get("next") == "review":
        return redirect(url_for("portfolios.review_members", portfolio_id=portfolio_id))
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="installations"))


@portfolio_bp.get("/portfolios/<int:portfolio_id>/members/review")
@require_authenticated
def review_members(portfolio_id: int) -> str:
    """Every open member with no installation, one decision at a time.

    This is the simple, auditable path the settings page's bare "type an ID"
    form was not: a NIF that exactly matches a V2 organization and a name
    search are offered as evidence, and resolving still takes an explicit
    choice from whoever is looking, recorded with their name.
    """
    session = get_request_session()
    try:
        context = member_review_data(session, portfolio_id=portfolio_id)
    except ValueError:
        abort(404)
    return render_template(
        "portfolios/review_members.html",
        title=f"Membros por resolver — {context['portfolio'].name}",
        csrf_token=token(),
        **context,
    )


@portfolio_bp.post("/portfolios/<int:portfolio_id>/members/<int:membership_id>/resolve")
@require_authenticated
def resolve_member(portfolio_id: int, membership_id: int):
    require_valid_token()
    session = get_request_session()
    try:
        asset_id = int(request.form.get("asset_id", "").strip())
    except ValueError:
        flash("Indica a instalação.", "error")
        return _member_redirect(portfolio_id)
    actor = _actor()
    try:
        with session.begin():
            resolve_member_to_asset(session, membership_id=membership_id, asset_id=asset_id, resolved_by=actor)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Membro associado à instalação.", "success")
    return _member_redirect(portfolio_id)


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
                created_by=_actor(),
            )
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Filtro aplicado ao portfolio.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="settings"))


# --- the monthly workflow: validar cobertura -> gerar -> rever -> aprovar ---


@portfolio_bp.post("/portfolios/<int:portfolio_id>/reports/generate")
@require_authenticated
def generate_reports(portfolio_id: int):
    """Freeze the period, then generate a report for every ready member.

    Safe to run again before approval: it rebuilds against whatever facts exist
    now and replaces the run's members, sending any prior review back to
    `generated` since it was a statement about numbers that no longer hold.
    """
    require_valid_token()
    session = get_request_session()
    report_month = request.form.get("month", "").strip()
    try:
        with session.begin():
            run = generate_report_run(session, portfolio_id=portfolio_id, report_month=report_month, actor=_actor())
            ready = sum(1 for member in run.members if member.status == "ready")
            blocked = sum(1 for member in run.members if member.status == "blocked")
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash(f"Gerados {ready} relatórios individuais; {blocked} instalações bloqueadas.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="reports", month=report_month))


@portfolio_bp.post("/portfolios/<int:portfolio_id>/reports/<int:run_id>/review")
@require_authenticated
def review_run(portfolio_id: int, run_id: int):
    require_valid_token()
    session = get_request_session()
    report_month = request.form.get("month", "").strip()
    try:
        with session.begin():
            mark_run_reviewed(
                session, run_id=run_id, actor=_actor(), notes=request.form.get("notes", "").strip() or None
            )
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Período marcado como revisto.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="reports", month=report_month))


@portfolio_bp.post("/portfolios/<int:portfolio_id>/reports/<int:run_id>/approve")
@require_authenticated
def approve_run_route(portfolio_id: int, run_id: int):
    require_valid_token()
    session = get_request_session()
    report_month = request.form.get("month", "").strip()
    try:
        with session.begin():
            approve_run(session, run_id=run_id, actor=_actor())
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Período aprovado. O registo fica bloqueado a partir de agora.", "success")
    return redirect(url_for("portfolios.detail", portfolio_id=portfolio_id, section="reports", month=report_month))
