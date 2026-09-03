"""Produção: the fleet-wide production screen -- GOAL.md's nav puts it
under Performance, as a sibling of ESCO and Relatórios, not folded into
either. See `production_queries.py` for what it reads and why."""
from __future__ import annotations

from flask import Blueprint, render_template

from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.production_queries import production_page

production_bp = Blueprint("production", __name__, url_prefix="/producao")


@production_bp.get("")
@require_authenticated
def index() -> str:
    session = get_request_session()
    return render_template("production/index.html", title="Produção", **production_page(session))
