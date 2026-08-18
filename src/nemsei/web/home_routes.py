from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from flask import Blueprint, redirect, render_template, request, session, url_for

from nemsei.web.csrf import token
from nemsei.web.db_session import get_request_session
from nemsei.web.queries import dashboard_data

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
    return render_template(
        "dashboard.html",
        title="Visão geral",
        dashboard=dashboard_data(get_request_session()),
        csrf_token=token(),
    )
