from __future__ import annotations

from sqlalchemy import text

from nemsei.db import build_engine


def test_postgres_engine_applies_configured_timeouts(settings) -> None:
    with build_engine(settings).connect() as connection:
        assert connection.execute(text("SHOW statement_timeout")).scalar() == "15s"
        assert connection.execute(text("SHOW lock_timeout")).scalar() == "3s"
