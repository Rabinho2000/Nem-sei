from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from flask import Blueprint, redirect, render_template, request, session, url_for

from nemsei.web.csrf import token
from nemsei.web.db_session import get_request_session
from nemsei.web.panel import operational_panel
from nemsei.web.queries import dashboard_data
from nemsei.web.series import portfolio_monthly_series

home_bp = Blueprint("home", __name__)


def require_authenticated(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)

    return wrapped


@home_bp.get("/")
@require_authenticated
def index() -> str:
    # Deliberately not named `session`: this module imports Flask's `session`
    # for require_authenticated, and shadowing it here would leave a trap for
    # the next edit that reaches for the browser session inside this view.
    db = get_request_session()
    panel = operational_panel(db)
    return render_template(
        "dashboard.html",
        title="Painel",
        panel=panel,
        portfolio=portfolio_monthly_series(db, total_assets=panel["total_assets"]),
        dashboard=dashboard_data(db),
        csrf_token=token(),
    )
