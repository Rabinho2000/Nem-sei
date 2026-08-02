"""Lifecycle helpers for the in-process APScheduler instance.

The deployment deliberately runs a single Gunicorn worker.  The scheduler is
therefore application-owned rather than process-owned; this module keeps that
ownership explicit without changing existing job registration behaviour.
"""
from __future__ import annotations

from typing import Any

from flask import Flask


SCHEDULER_EXTENSION_KEY = "monitoring_board.scheduler"


def attached_scheduler(app: Flask) -> Any | None:
    """Return the scheduler registered for this Flask application."""

    return app.extensions.get(SCHEDULER_EXTENSION_KEY)


def attach_scheduler(app: Flask, scheduler: Any) -> None:
    """Attach a started scheduler to one Flask application instance."""

    app.extensions[SCHEDULER_EXTENSION_KEY] = scheduler


def detach_scheduler(app: Flask) -> None:
    """Forget a scheduler that is disabled for this application runtime."""

    app.extensions.pop(SCHEDULER_EXTENSION_KEY, None)
