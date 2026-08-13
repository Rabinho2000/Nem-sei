"""Database and Alembic readiness checks without schema mutation."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


ReadinessCheck = Callable[[], tuple[bool, str]]


def uninitialized_readiness() -> tuple[bool, str]:
    return False, "database schema is not initialized"


def database_readiness(engine: Engine) -> tuple[bool, str]:
    try:
        with engine.connect() as connection:
            current_revision = MigrationContext.configure(connection).get_current_revision()
        root = Path(__file__).resolve().parents[3]
        alembic_config = Config(str(root / "alembic.ini"))
        head_revision = ScriptDirectory.from_config(alembic_config).get_current_head()
    except Exception:
        return False, "database unavailable"
    if current_revision != head_revision:
        return False, "database schema is outdated"
    return True, "ready"
