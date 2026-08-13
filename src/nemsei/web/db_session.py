"""Lazy Flask request-session lifecycle for future HTTP adapters."""
from __future__ import annotations

from flask import current_app, g
from sqlalchemy.orm import Session


_SESSION_KEY = "nemsei.request_session"


def get_request_session() -> Session:
    """Create a short-lived request session only for code that asks for one."""
    session = g.get(_SESSION_KEY)
    if session is None:
        session = current_app.extensions["nemsei.session_factory"]()
        setattr(g, _SESSION_KEY, session)
    return session


def close_request_session(_exception: BaseException | None = None) -> None:
    session = g.pop(_SESSION_KEY, None)
    if session is not None:
        try:
            if session.in_transaction():
                session.rollback()
        finally:
            session.close()
