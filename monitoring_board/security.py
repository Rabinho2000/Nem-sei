from __future__ import annotations

import os
import secrets

from flask import session
from werkzeug.security import check_password_hash

INSECURE_SECRET_KEYS = {"", "change-me", "monitoring-board-local-secret"}


def app_username() -> str:
    return os.environ.get("APP_USERNAME", "admin").strip() or "admin"


def flask_secret_key() -> str:
    secret_key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if secret_key in INSECURE_SECRET_KEYS:
        raise RuntimeError(
            "Configura FLASK_SECRET_KEY no ficheiro .env com um valor secreto antes de arrancar a app."
        )
    return secret_key


def app_password_configured() -> bool:
    return bool(os.environ.get("APP_PASSWORD_HASH") or os.environ.get("APP_PASSWORD"))


def check_app_password(password: str) -> bool:
    password_hash = os.environ.get("APP_PASSWORD_HASH", "").strip()
    if password_hash:
        return check_password_hash(password_hash, password)
    plain_password = os.environ.get("APP_PASSWORD", "")
    return bool(plain_password) and secrets.compare_digest(password, plain_password)


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return str(token)
