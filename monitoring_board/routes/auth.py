from __future__ import annotations

from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from monitoring_board.security import app_password_configured, app_username, check_app_password, csrf_token


auth_bp = Blueprint("auth", __name__)


def safe_local_next_url(value: str | None) -> str:
    next_url = (value or "").strip()
    parsed = urlsplit(next_url)
    if parsed.path.startswith("/") and not parsed.netloc and not parsed.scheme:
        return next_url
    return url_for("dashboard")


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> str:
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not app_password_configured():
            flash("Configura APP_PASSWORD_HASH ou APP_PASSWORD no ficheiro .env antes de entrar.", "error")
            return render_template("login.html", title="Login", expected_username=app_username())
        if username == app_username() and check_app_password(password):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["username"] = username
            csrf_token()
            current_app.logger.info("Login successful for %s", username)
            return redirect(safe_local_next_url(request.form.get("next")))
        current_app.logger.warning("Login failed for %s", username or "<empty>")
        flash("Login invalido.", "error")
    return render_template("login.html", title="Login", expected_username=app_username())


@auth_bp.route("/logout", methods=["POST"])
def logout() -> str:
    username = session.get("username", "")
    session.clear()
    current_app.logger.info("Logout for %s", username or "<unknown>")
    return redirect(url_for("auth.login"))
