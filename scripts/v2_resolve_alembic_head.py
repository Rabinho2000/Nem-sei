#!/usr/bin/env python3
"""Resolve the single supported head of the checked-out Alembic graph."""
from __future__ import annotations

import argparse
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


class AlembicHeadError(RuntimeError):
    """Raised when the migration graph cannot identify one safe head."""


def resolve_single_head(config_path: str | Path) -> str:
    config = Config(str(config_path))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if not heads:
        raise AlembicHeadError("Alembic migration graph has no head.")
    if len(heads) != 1:
        raise AlembicHeadError(
            f"Alembic migration graph has {len(heads)} heads; multiple heads are unsupported."
        )
    return heads[0]


def validate_restored_revision(restored_revision: str | None, expected_head: str) -> None:
    revision = (restored_revision or "").strip()
    if not revision:
        raise AlembicHeadError("Restored database has no Alembic revision.")
    if revision != expected_head:
        raise AlembicHeadError(
            f"Restored Alembic revision '{revision}' does not match repository head '{expected_head}'."
        )


def validate_live_revision(live_revision: str | None, expected_head: str) -> None:
    """Fail loudly when a migration left the database behind the checked-out graph.

    A stale migrate image runs `alembic upgrade head` against its own older
    graph, reports success, and leaves the database on an earlier revision.
    """
    revision = (live_revision or "").strip()
    if not revision:
        raise AlembicHeadError(
            "Migrated database reports no Alembic revision; the migration did not run."
        )
    if revision != expected_head:
        raise AlembicHeadError(
            f"Migrated database is at Alembic revision '{revision}' but the checked-out "
            f"repository head is '{expected_head}'. The migrate image is stale or the "
            "migration did not apply; rebuild it and migrate again before serving traffic."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--live-revision",
        help="Verify this database revision equals the resolved head and fail if it does not.",
    )
    args = parser.parse_args()
    try:
        head = resolve_single_head(args.config)
        if args.live_revision is not None:
            validate_live_revision(args.live_revision, head)
        print(f"__NEMSEI_ALEMBIC_HEAD__={head}")
    except AlembicHeadError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
