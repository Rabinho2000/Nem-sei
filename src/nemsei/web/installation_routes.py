"""The Installation-first operational screens: list and detail with tabs.

New surface at `/instalacoes`, alongside the existing `/assets` admin
screens -- not a replacement. `/assets` stays where identity, aliases,
mappings and commercial configuration are edited; `/instalacoes` is where an
operator asks "what is happening, what needs attention, what are we doing
about it". See `installation_queries.py` for why both read through `Asset`.
"""
from __future__ import annotations

from flask import Blueprint, abort, render_template, request

from nemsei.contracts.priority import COMMERCIAL_FAMILIES, FAMILY_LABELS
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.installation_queries import installation_detail, installation_list_rows
from nemsei.web.series import PERIOD_LABELS, PRODUCTION_CONSUMPTION_PERIODS
from nemsei.web.work_order_queries import planning_page, work_orders_page

installations_bp = Blueprint("installations", __name__, url_prefix="/instalacoes")
work_orders_bp = Blueprint("work_orders", __name__, url_prefix="/trabalhos")
# A sibling of `work_orders_bp`, not a sub-path of it: GOAL.md's nav lists
# "Trabalhos" and "Planeamento" as two separate items under Operação, and
# the flat "every work order" list answers a different question ("o que
# existe") than the bucketed one here ("o que fazer esta semana").
planning_bp = Blueprint("planning", __name__, url_prefix="/planeamento")


@installations_bp.get("")
@require_authenticated
def index() -> str:
    session = get_request_session()
    return render_template(
        "installations/list.html",
        title="Instalações",
        family_options=[(value, FAMILY_LABELS[value]) for value in COMMERCIAL_FAMILIES],
        **installation_list_rows(
            session,
            search=request.args.get("search", "").strip(),
            needs_review=request.args.get("needs_review", "").strip(),
            provider=request.args.get("provider", "").strip(),
            mapping=request.args.get("mapping", "").strip(),
            om=request.args.get("om", "todos").strip(),
            family=request.args.get("family", "").strip(),
            page_value=request.args.get("page"),
        ),
    )


@installations_bp.get("/<int:asset_id>")
@require_authenticated
def detail(asset_id: int) -> str:
    session = get_request_session()
    tab = request.args.get("tab", "resumo").strip()
    period = request.args.get("period", "week").strip()
    context = installation_detail(session, asset_id=asset_id, tab=tab, period=period)
    if context is None:
        abort(404)
    return render_template(
        "installations/detail.html",
        title=context["asset"].canonical_name,
        period_options=[(value, PERIOD_LABELS[value]) for value in PRODUCTION_CONSUMPTION_PERIODS],
        **context,
    )


@work_orders_bp.get("")
@require_authenticated
def index_work_orders() -> str:
    session = get_request_session()
    return render_template(
        "work_orders/list.html",
        title="Trabalhos",
        **work_orders_page(
            session,
            status=request.args.get("status", "").strip(),
            scope=request.args.get("scope", "").strip(),
            search=request.args.get("search", "").strip(),
        ),
    )


@planning_bp.get("")
@require_authenticated
def index_planning() -> str:
    session = get_request_session()
    return render_template("planning/index.html", title="Planeamento", **planning_page(session))
