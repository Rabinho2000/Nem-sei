"""Classify sync runs whose owner can no longer finish them.

A `SyncRun` is opened by one process and closed by that same process. Nothing
else can close it: `finish_sync_run` refuses a run that is not `running`, and
there is no lease, no owner column and no recovery pass. So a process that dies
between the two -- a container restarted mid-sync, a manual rollout script
interrupted at a terminal -- leaves a row that says `running` forever.

Two such rows were still open on 2026-08-31: run 4 (FusionSolar production,
opened 2026-08-19, one day fetched and then nothing) and run 43 (Sigenergy
production, opened 2026-08-25, authenticated and then nothing). Both were
opened by hand during a rollout, and neither has any chance of being finished.

The operational cost is not a lock -- nothing blocks on them -- it is that
every question of the form "how did the last run of this capability go?" has to
decide what a nine-day-old `running` means, and the honest answer is not
available from the row. So this module writes the answer down instead of
leaving each reader to guess.

Nothing here rewrites history. An abandoned run keeps its `started_at`, its
metadata and every fact it wrote; it gains a terminal status, an `abandoned`
error code, and a `finished_at` set to the last moment it is *known* to have
been alive rather than to the moment it was noticed. It also deliberately does
not touch `integration_health`: that describes the connection now, and a run
that stopped nine days ago is not news about now.

This module stays provider-neutral, like the rest of `nemsei.sync`. It knows
one kind of liveness evidence -- provider request attempts, which every
outbound capability writes -- and takes any other kind as an injected
`OwnerLiveness`. Huawei SCADA needs that: its runs make no outbound call at
all, so their heartbeat lives in the adapter's own session rows, and only the
adapter should know that.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.shared.clock import as_utc, utc_now
from nemsei.sync.models import ProviderRequestAttempt, SyncRun


# How long a run may show no evidence of life before it is classified as
# abandoned. This is not a guess at how long a sync takes: a run that is
# working writes a `provider_request_attempt` for every provider call, roughly
# twice a second, and a run whose evidence lives elsewhere brings its own
# `OwnerLiveness` to say so. An hour of complete silence is past anything
# either mechanism can produce while still running.
DEFAULT_SILENCE_GRACE = timedelta(hours=1)

ABANDONED_ERROR_CODE = "abandoned"


@dataclass(frozen=True)
class OwnerLiveness:
    """What one owner-aware resolver knows about a run's owner.

    `owner_ended` is the strong answer and needs no waiting: the owner did not
    merely go quiet, it reached its own end without closing the run, which is
    a state only an abandoned run can be in. `last_alive_at` is the weak one,
    folded into the silence test with every other piece of evidence.
    """

    last_alive_at: datetime | None = None
    owner_ended: bool = False
    reason: str | None = None


# A resolver answers for the runs it owns and returns None for every other.
OwnerResolver = Callable[[Session, SyncRun], OwnerLiveness | None]


@dataclass(frozen=True)
class AbandonedRun:
    run_id: int
    capability: str
    provider_connection_id: int
    started_at: datetime
    last_alive_at: datetime
    reason: str


@dataclass(frozen=True)
class AbandonmentSweep:
    examined: int
    abandoned: tuple[AbandonedRun, ...]

    @property
    def abandoned_count(self) -> int:
        return len(self.abandoned)


def _last_attempt_at(session: Session, run_id: int) -> datetime | None:
    return session.scalar(
        select(func.max(ProviderRequestAttempt.occurred_at)).where(ProviderRequestAttempt.sync_run_id == run_id)
    )


def classify(
    session: Session,
    run: SyncRun,
    *,
    now: datetime,
    silence_grace: timedelta = DEFAULT_SILENCE_GRACE,
    owner_resolvers: Sequence[OwnerResolver] = (),
) -> AbandonedRun | None:
    """Decide whether one running sync run has provably lost its owner."""
    started = as_utc(run.started_at)
    last_alive = started
    reason = "the run made no provider call at all after opening"

    owner_reason = False
    for resolve in owner_resolvers:
        liveness = resolve(session, run)
        if liveness is None:
            continue
        if liveness.owner_ended:
            return AbandonedRun(
                run_id=run.id,
                capability=run.capability,
                provider_connection_id=run.provider_connection_id,
                started_at=started,
                last_alive_at=as_utc(liveness.last_alive_at) if liveness.last_alive_at else started,
                reason=liveness.reason or "the owner of this run ended without finishing it",
            )
        if liveness.last_alive_at is not None:
            last_alive = max(last_alive, as_utc(liveness.last_alive_at))
        if liveness.reason:
            reason = liveness.reason
            owner_reason = True

    attempt_at = _last_attempt_at(session, run.id)
    if attempt_at is not None:
        last_alive = max(last_alive, as_utc(attempt_at))
        if not owner_reason:
            # It did work, and then stopped. Saying "no evidence of activity"
            # about a run that fetched a day and then died reads as though it
            # never started, which sends someone looking in the wrong place.
            reason = "the run stopped making provider calls and never finished"

    if now - last_alive <= silence_grace:
        return None
    return AbandonedRun(
        run_id=run.id,
        capability=run.capability,
        provider_connection_id=run.provider_connection_id,
        started_at=started,
        last_alive_at=last_alive,
        reason=reason,
    )


def sweep_abandoned_sync_runs(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
    silence_grace: timedelta = DEFAULT_SILENCE_GRACE,
    owner_resolvers: Sequence[OwnerResolver] = (),
) -> AbandonmentSweep:
    """Close every running sync run whose owner is provably gone.

    One transaction per run, and each run is re-read and re-checked inside it,
    so a run that a live process finishes in between is left exactly alone --
    the same restart-safe shape `deliver_pending_notifications` uses, and for
    the same reason: this runs on a schedule, concurrently with the processes
    that own these rows.
    """
    now_value = as_utc(now or utc_now())
    with session_factory() as session:
        candidate_ids = list(session.scalars(select(SyncRun.id).where(SyncRun.status == "running").order_by(SyncRun.id)))

    abandoned: list[AbandonedRun] = []
    for run_id in candidate_ids:
        with session_factory() as session, session.begin():
            run = session.get(SyncRun, run_id, with_for_update=True)
            if run is None or run.status != "running":
                continue  # finished by its owner between the scan and now
            verdict = classify(
                session, run, now=now_value, silence_grace=silence_grace, owner_resolvers=owner_resolvers
            )
            if verdict is None:
                continue
            run.status = "failed"
            run.error_code = ABANDONED_ERROR_CODE
            # `finished_at` is the last moment the run is known to have been
            # alive, not the moment it was noticed. A run that stopped on the
            # 19th did not run until the 31st, and saying so would make every
            # duration derived from these rows wrong.
            run.finished_at = verdict.last_alive_at
            run.safe_detail = (
                f"Classified abandoned: {verdict.reason}. The process that opened this run "
                "is no longer able to finish it."
            )
            run.metadata_json = {
                **(run.metadata_json or {}),
                "abandoned": True,
                "abandoned_reason": verdict.reason,
                "abandoned_detected_at": now_value.isoformat(),
                "last_alive_at": verdict.last_alive_at.isoformat(),
            }
            # Deliberately no `record_health` call: integration health answers
            # "how is this connection right now", and a run that went quiet
            # days ago is not an answer to that. `finish_sync_run` is skipped
            # for the same reason -- it exists for an owner closing its own
            # run, which is exactly what did not happen here.
            abandoned.append(verdict)

    return AbandonmentSweep(examined=len(candidate_ids), abandoned=tuple(abandoned))
