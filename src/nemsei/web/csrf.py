"""Small session-backed CSRF protection for V2 form routes."""
from __future__ import annotations

import secrets

from flask import abort, request, session


def token() -> str:
    value = session.get("csrf_token")
    if not value:
        value = secrets.token_urlsafe(32)
        session["csrf_token"] = value
    return str(value)


def require_valid_token() -> None:
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not sent or not secrets.compare_digest(sent, token()):
        abort(400)
