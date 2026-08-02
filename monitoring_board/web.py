"""Flask setup shared by the application factory and future route modules."""
from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, abort, current_app, g, redirect, render_template, request, session, url_for

from monitoring_board.db import get_db
from monitoring_board.logging_config import configure_logging
from monitoring_board.security import csrf_token, flask_secret_key


TemplateGlobals = Callable[[], dict[str, Any]]


def configure_web_application(
    app: Flask,
    *,
    data_dir: Path,
    database: Path,
    excel_path: Path | None,
    log_dir: Path,
    max_content_length: int,
    preview_banner: bool,
    external_actions_enabled: bool,
    template_globals: TemplateGlobals,
) -> None:
    """Install common configuration, request lifecycle and error responses."""

    app.config.update(
        SECRET_KEY=flask_secret_key(),
        DATA_DIR=str(data_dir),
        DATABASE=str(database),
        EXCEL_PATH=str(excel_path) if excel_path else "",
        MAX_CONTENT_LENGTH=max_content_length,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=os.environ.get("SESSION_COOKIE_SAMESITE", "Lax").strip() or "Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on", "sim"},
        PREVIEW_BANNER=preview_banner,
        EXTERNAL_ACTIONS_ENABLED=external_actions_enabled,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    configure_logging(app, log_dir)
    app.logger.info("Using database at %s", app.config["DATABASE"])

    @app.before_request
    def open_request_database_and_require_login() -> Any:
        g.db = get_db(app.config["DATABASE"])
        g.request_started_at = datetime.now()
        if request.method == "POST":
            sent_token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not sent_token or not secrets.compare_digest(sent_token, csrf_token()):
                app.logger.warning("CSRF validation failed for %s %s", request.method, request.path)
                abort(400)
        if request.endpoint not in {"auth.login", "static"} and not session.get("authenticated"):
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        return None

    @app.teardown_request
    def close_request_database(exception: BaseException | None) -> None:
        started_at = getattr(g, "request_started_at", None)
        elapsed_ms = ""
        if started_at:
            elapsed_ms = f" {(datetime.now() - started_at).total_seconds() * 1000:.0f}ms"
        if request.endpoint != "static":
            if exception:
                app.logger.exception("%s %s failed%s", request.method, request.path, elapsed_ms)
            else:
                app.logger.info("%s %s%s", request.method, request.path, elapsed_ms)
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "today_iso": date.today().isoformat(),
            **template_globals(),
            "csrf_token": csrf_token,
            "current_username": session.get("username"),
            "preview_banner": app.config["PREVIEW_BANNER"],
            "external_actions_enabled": app.config["EXTERNAL_ACTIONS_ENABLED"],
        }

    @app.errorhandler(400)
    def bad_request_error(error: Exception) -> tuple[str, int]:
        return render_template(
            "error.html",
            title="Pedido invalido",
            heading="Pedido invalido",
            message="A acao nao foi aceite. Atualiza a pagina e tenta novamente.",
        ), 400

    @app.errorhandler(404)
    def not_found_error(error: Exception) -> tuple[str, int]:
        return render_template(
            "error.html",
            title="Pagina nao encontrada",
            heading="Pagina nao encontrada",
            message="Nao encontrei esta pagina ou registo.",
        ), 404

    @app.errorhandler(500)
    def internal_error(error: Exception) -> tuple[str, int]:
        current_app.logger.exception("Unhandled application error")
        return render_template(
            "error.html",
            title="Erro interno",
            heading="Erro interno",
            message="Aconteceu um erro inesperado. Consulta os logs para o detalhe tecnico.",
        ), 500

    @app.errorhandler(413)
    def request_too_large_error(error: Exception) -> tuple[str, int]:
        return render_template(
            "error.html",
            title="Ficheiro demasiado grande",
            heading="Ficheiro demasiado grande",
            message="O ficheiro enviado excede o limite configurado para uploads.",
        ), 413
