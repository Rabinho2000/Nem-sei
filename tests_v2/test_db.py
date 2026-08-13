from __future__ import annotations

from nemsei.db import build_engine, build_session_factory


def test_sqlite_engine_applies_required_pragmas(settings) -> None:
    settings.data_root.mkdir()
    engine = build_engine(settings)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 15000
    assert build_session_factory(engine)
