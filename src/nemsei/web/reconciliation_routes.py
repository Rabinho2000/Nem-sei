from __future__ import annotations

from flask import Blueprint, render_template

from nemsei.web.csrf import token
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.queries import reconciliation_data


reconciliation_bp = Blueprint("reconciliation", __name__)


@reconciliation_bp.get("/reconciliation")
@require_authenticated
def index() -> str:
    return render_template(
        "reconciliation.html",
        title="Revisão de dados",
        review=reconciliation_data(get_request_session()),
        csrf_token=token(),
    )
