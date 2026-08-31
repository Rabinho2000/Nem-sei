"""Where a Huawei SCADA sync run's liveness evidence actually lives.

Every other capability in this system proves a run is alive by the provider
calls it makes. This one makes none: the dongle dials in and the listener
answers, so there is no outbound request to record. The heartbeat is the
session row instead -- `last_seen_at`, updated on every poll -- and the link
between a run and its session is the `sync_run_id` the listener writes into the
session's metadata when it starts polling (`ingestion.py`).

That knowledge belongs here rather than in `nemsei.sync`, which stays
provider-neutral: it takes this as an injected resolver.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.integrations.huawei_scada.models import HuaweiScadaSession
from nemsei.sync.abandonment import OwnerLiveness
from nemsei.sync.models import SyncRun


def scada_session_liveness(session: Session, run: SyncRun) -> OwnerLiveness | None:
    """The dongle session that owns this run, or None if none does.

    A closed session with a still-running run is the strong verdict: the owner
    reached its own end and did not close the run, which cannot happen while
    anything is still working on it.
    """
    owner = session.scalar(
        select(HuaweiScadaSession).where(
            HuaweiScadaSession.metadata_json["sync_run_id"].as_integer() == run.id
        )
    )
    if owner is None:
        return None
    if owner.closed_at is not None:
        return OwnerLiveness(
            last_alive_at=owner.closed_at,
            owner_ended=True,
            reason="the dongle session that owned this run closed without finishing it",
        )
    return OwnerLiveness(
        last_alive_at=owner.last_seen_at,
        reason="the dongle session that owned this run stopped reporting",
    )
