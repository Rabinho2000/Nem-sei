"""Readiness contract; database/Alembic wiring is added with persistence."""
from __future__ import annotations

from collections.abc import Callable


ReadinessCheck = Callable[[], tuple[bool, str]]


def uninitialized_readiness() -> tuple[bool, str]:
    return False, "database schema is not initialized"
