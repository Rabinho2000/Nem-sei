"""SQLite engine construction and connection-local safety settings."""
from __future__ import annotations

from sqlalchemy import Engine, event
from sqlalchemy.engine import create_engine
from sqlalchemy.pool import NullPool

from nemsei.config import Settings


def build_engine(settings: Settings) -> Engine:
    settings.validate()
    engine = create_engine(
        settings.database_url,
        connect_args={"timeout": 15},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 15000")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA temp_store = MEMORY")
        finally:
            cursor.close()

    return engine
