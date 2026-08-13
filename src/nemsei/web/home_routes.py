from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from flask import Blueprint, redirect, render_template, request, session, url_for


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
    return render_template("base.html", title="Nem-sei V2")
