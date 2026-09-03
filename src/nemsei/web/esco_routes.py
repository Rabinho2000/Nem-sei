"""ESCO: the fleet-wide autoconsumo/poupança/receita Solcor screen. See
`esco_queries.py` for the exact definition of "instalação ESCO" this page
uses -- the same one `calculate_billing` uses, not a second guess."""
from __future__ import annotations

from flask import Blueprint, render_template

from nemsei.web.db_session import get_request_session
from nemsei.web.esco_queries import esco_page
from nemsei.web.home_routes import require_authenticated

esco_bp = Blueprint("esco", __name__, url_prefix="/esco")


@esco_bp.get("")
@require_authenticated
def index() -> str:
    session = get_request_session()
    return render_template("esco/index.html", title="ESCO", **esco_page(session))
