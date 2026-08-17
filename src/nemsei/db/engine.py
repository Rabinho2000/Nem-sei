"""PostgreSQL engine construction; V1 SQLite is importer-only."""
from __future__ import annotations

from sqlalchemy import Engine, event
from sqlalchemy.engine import create_engine

from nemsei.config import Settings


def build_engine(settings: Settings) -> Engine:
    settings.validate()
    engine = create_engine(settings.sqlalchemy_database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow, pool_pre_ping=True, pool_recycle=settings.db_pool_recycle_seconds)

    @event.listens_for(engine, "connect")
    def configure_postgres_connection(connection, _record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SET statement_timeout = {settings.db_statement_timeout_ms}")
            cursor.execute(f"SET lock_timeout = {settings.db_lock_timeout_ms}")
            cursor.execute(f"SET idle_in_transaction_session_timeout = {settings.db_idle_transaction_timeout_ms}")
        finally:
            cursor.close()
    return engine
