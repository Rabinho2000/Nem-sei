"""Which provider connections have a scheduled production sync, and in what mode.

The scheduler used to answer this with a single environment variable, and
the comment beside it said why: a shared, rate-limited FusionSolar account
is not a place to loop over whatever connections happen to exist. That
restraint is right and this module keeps it -- what it changes is *where*
the list comes from. Eligibility is now a persisted, per-connection fact
(`provider_connections.production_sync_enabled`, default `false`), so
running a second account is an operator decision recorded in the database
rather than a deploy-time environment variable, and adding a connection
still starts nothing.

What is deliberately **not** here: any form of "for connection in
all_connections". A connection that nobody turned on is not a target, and
`enabled`/`configured` are checked on top of that, so three separate facts
have to line up before a single provider call is scheduled.

Each target keeps everything per connection -- its own `ScheduleState` key
(`production.incremental:{id}`, already the shape `jobs/repository.py`
uses), its own dedupe key, its own cursor (`sync_cursors` is keyed by
connection), its own cooldown (`provider_request_states` likewise) and its
own row in the automation health screen. Two connections cannot collide
because nothing they touch is shared.

Scoped to FusionSolar. Sigenergy keeps its existing single-connection
environment path untouched: its production service has no bounded-backfill
mode at all (`jobs/handlers._execute_sigenergy_production` refuses
anything but incremental), so the bootstrap half of this module has nothing
to offer it and pretending otherwise would enqueue jobs that can only fail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.providers.models import ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.sync.models import SyncCursor


# The cursor key the FusionSolar production service writes. Duplicated here
# rather than imported: `nemsei.sync` must not depend on
# `nemsei.integrations` (`test_architecture_boundaries`), and the constant is
# pinned by `test_production_scheduling.py` against the adapter's own so the
# two cannot drift apart silently.
PRODUCTION_CURSOR_KEY = "fusionsolar-daily-production"

# A connection with a cursor runs incremental; without one it cannot, because
# `FusionSolarProductionService._window` refuses to invent a first start date
# ("The first production sync requires an explicit start date"). These are the
# three honest answers to "what should this connection do next".
MODE_INCREMENTAL = "incremental"
MODE_BOOTSTRAP = "bootstrap"
MODE_NOT_INITIALIZED = "not_initialized"


@dataclass(frozen=True)
class ProductionScheduleTarget:
    """One connection's standing production schedule, fully self-contained."""

    connection_id: int
    display_name: str
    mode: str
    interval_hours: int
    # Bootstrap only: the first day to fetch. `None` for every other mode.
    start_date: date | None = None
    # The cursor's own account of itself, so the coverage screen does not have
    # to re-read `sync_cursors` to explain a mode.
    last_completed_day: date | None = None
    # True when a cursor exists but is further behind than one incremental run
    # is allowed to cover. The job still runs and still fails loudly, exactly
    # as it does today -- this flag is what lets the coverage screen name the
    # condition and recommend the bounded backfill, instead of an operator
    # reading "window exceeds the configured normal-sync safety limit" and
    # having to work out what to do about it.
    cursor_stale: bool = False

    @property
    def schedule_key(self) -> str:
        """Per connection, so two connections never share a slot or a dedupe key."""
        return f"production.{self.mode}:{self.connection_id}"


def _cursor_day(checkpoint: dict) -> date | None:
    value = checkpoint.get("last_completed_day")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        # A malformed checkpoint is the service's problem to raise on, not
        # this module's to guess around; treated as "no usable cursor", which
        # produces a bootstrap or a not-initialised state rather than an
        # incremental run against a date nobody can read.
        return None


def production_schedule_targets(
    session: Session,
    *,
    default_interval_hours: int,
    max_incremental_gap_days: int,
    legacy_connection_id: int | None = None,
    today: date | None = None,
) -> list[ProductionScheduleTarget]:
    """Every FusionSolar connection with a standing production schedule.

    `legacy_connection_id` is the environment variable the single-connection
    rollout used. It is folded in as an *additional* eligible connection, not
    as a replacement for the column, so a deployment that has not yet set
    `production_sync_enabled` on anything keeps syncing exactly what it syncs
    today. Removing it later is a configuration change, not a code change.

    `max_incremental_gap_days` is `production_max_source_days` -- the limit
    `_window` applies to an incremental run. A cursor further behind than
    that cannot be caught up by the incremental path at all; the target is
    still returned as `incremental` (behaviour unchanged, and it fails
    loudly the way it does now) but flagged `cursor_stale` so the coverage
    screen can say so and name the fix.
    """
    reference = today or date.today()
    statement = select(ProviderConnection).where(
        ProviderConnection.provider_code == ProviderCode.FUSIONSOLAR.value,
        ProviderConnection.enabled.is_(True),
        ProviderConnection.configuration_status == "configured",
    )
    if legacy_connection_id is not None:
        statement = statement.where(
            (ProviderConnection.production_sync_enabled.is_(True))
            | (ProviderConnection.id == legacy_connection_id)
        )
    else:
        statement = statement.where(ProviderConnection.production_sync_enabled.is_(True))
    connections = list(session.scalars(statement.order_by(ProviderConnection.id)).all())
    if not connections:
        return []

    cursors = {
        row.provider_connection_id: dict(row.checkpoint_json or {})
        for row in session.scalars(
            select(SyncCursor).where(
                SyncCursor.provider_connection_id.in_([connection.id for connection in connections]),
                SyncCursor.capability == ProviderCapability.PRODUCTION_HISTORY.value,
                SyncCursor.cursor_key == PRODUCTION_CURSOR_KEY,
            )
        ).all()
    }

    targets: list[ProductionScheduleTarget] = []
    for connection in connections:
        interval = connection.production_sync_interval_hours or default_interval_hours
        last_day = _cursor_day(cursors[connection.id]) if connection.id in cursors else None
        if last_day is not None:
            targets.append(
                ProductionScheduleTarget(
                    connection_id=connection.id,
                    display_name=connection.display_name,
                    mode=MODE_INCREMENTAL,
                    interval_hours=interval,
                    last_completed_day=last_day,
                    cursor_stale=(reference - timedelta(days=1) - last_day).days > max_incremental_gap_days,
                )
            )
        elif connection.initial_production_from_date is not None:
            targets.append(
                ProductionScheduleTarget(
                    connection_id=connection.id,
                    display_name=connection.display_name,
                    mode=MODE_BOOTSTRAP,
                    interval_hours=interval,
                    start_date=connection.initial_production_from_date,
                )
            )
        else:
            # No cursor and no stated first day. Nothing is scheduled and no
            # provider call is made: the alternative would be guessing how far
            # back this account's history goes, and a wrong guess is either a
            # year of calls nobody asked for or a silent hole at the start.
            targets.append(
                ProductionScheduleTarget(
                    connection_id=connection.id,
                    display_name=connection.display_name,
                    mode=MODE_NOT_INITIALIZED,
                    interval_hours=interval,
                )
            )
    return targets
