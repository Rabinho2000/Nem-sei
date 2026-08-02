"""Public entry points for idempotent SQLite schema initialization.

The concrete migration sequence still lives with the application domain while
it is being split into repositories.  Keeping the public contract here avoids
the former module-level ``schema -> app_factory`` circular import and gives
callers one stable place to initialise a database.
"""
from __future__ import annotations

from collections.abc import Callable


SchemaInitializer = Callable[[str], None]
SchemaIndexInitializer = Callable[[object], None]

_initializer: SchemaInitializer | None = None
_index_initializer: SchemaIndexInitializer | None = None


def register_schema_initializer(
    initializer: SchemaInitializer,
    index_initializer: SchemaIndexInitializer,
) -> None:
    """Register the application migration implementation.

    Registration occurs before the Flask app is constructed.  The fallback is
    intentionally lazy so CLI callers importing this module directly remain
    compatible during the incremental extraction.
    """

    global _initializer, _index_initializer
    _initializer = initializer
    _index_initializer = index_initializer


def _load_legacy_initializer() -> None:
    if _initializer is not None:
        return
    # This is deliberately function-local: importing ``schema`` must never
    # import Flask nor trigger application bootstrap.
    from monitoring_board import app_factory

    register_schema_initializer(
        app_factory.ensure_database,
        app_factory.ensure_database_indexes,
    )


def ensure_database(path: str) -> None:
    """Create or upgrade a database without deleting existing data."""

    _load_legacy_initializer()
    assert _initializer is not None
    _initializer(path)


def ensure_database_indexes(conn: object) -> None:
    """Ensure the non-destructive indexes used by the application."""

    _load_legacy_initializer()
    assert _index_initializer is not None
    _index_initializer(conn)


__all__ = [
    "ensure_database",
    "ensure_database_indexes",
    "register_schema_initializer",
]
