from __future__ import annotations

from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from nemsei.security import verify_administrator_password
from nemsei.web.csrf import require_valid_token, token


auth_bp = Blueprint("auth", __name__)


def safe_next_url(value: str | None) -> str:
    parsed = urlsplit((value or "").strip())
    if parsed.path.startswith("/") and not parsed.netloc and not parsed.scheme:
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return url_for("home.index")


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> str:
    if request.method == "POST":
        require_valid_token()
        settings = current_app.extensions["nemsei.settings"]
        username = request.form.get("username", "").strip()
        if verify_administrator_password(settings, username, request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["username"] = username
            token()
            return redirect(safe_next_url(request.form.get("next")))
        flash("Login inválido.", "error")
    return render_template("auth/login.html", csrf_token=token(), next=request.args.get("next", ""))


@auth_bp.post("/logout")
def logout() -> str:
    require_valid_token()
    session.clear()
    return redirect(url_for("auth.login"))
