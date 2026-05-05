from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from monitoring_board.security import app_password_configured, app_username, check_app_password, csrf_token


auth_bp = Blueprint("auth", __name__)


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
            session["authenticated"] = True
            session["username"] = username
            csrf_token()
            current_app.logger.info("Login successful for %s", username)
            next_url = request.form.get("next") or url_for("dashboard")
            if not next_url.startswith("/"):
                next_url = url_for("dashboard")
            return redirect(next_url)
        current_app.logger.warning("Login failed for %s", username or "<empty>")
        flash("Login invalido.", "error")
    return render_template("login.html", title="Login", expected_username=app_username())


@auth_bp.route("/logout", methods=["POST"])
def logout() -> str:
    username = session.get("username", "")
    session.clear()
    current_app.logger.info("Logout for %s", username or "<unknown>")
    return redirect(url_for("auth.login"))
