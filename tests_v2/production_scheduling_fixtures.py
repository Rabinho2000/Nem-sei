"""Shared fixtures for production scheduling, so two test modules agree.

A production cursor is what separates "this connection can run an
incremental sync" from "this connection has never synced and cannot start
one". Several tests need to put a connection on one side of that line or
the other; writing the row by hand in each of them is how two tests end up
seeding subtly different cursors and only one of them proves anything.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from nemsei.providers.registry import ProviderCapability
from nemsei.shared.clock import utc_now
from nemsei.sync.models import SyncCursor
from nemsei.sync.production_scheduling import PRODUCTION_CURSOR_KEY


def seed_production_cursor(session: Session, *, connection_id: int, last_completed_day: date) -> SyncCursor:
    """The cursor `FusionSolarProductionService` leaves after a successful run.

    Only `last_completed_day` matters to the scheduler; `covered_through` is
    written to match so a row read back by anything else is coherent rather
    than half-filled.
    """
    cursor = SyncCursor(
        provider_connection_id=connection_id,
        capability=ProviderCapability.PRODUCTION_HISTORY.value,
        cursor_key=PRODUCTION_CURSOR_KEY,
        checkpoint_json={"last_completed_day": last_completed_day.isoformat(), "source_timezone": "Europe/Lisbon"},
        covered_through=datetime.combine(last_completed_day + timedelta(days=1), time.min, tzinfo=timezone.utc),
        updated_at=utc_now(),
    )
    session.add(cursor)
    session.flush()
    return cursor
