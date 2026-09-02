"""PostgreSQL-backed persisted queue operations; no handler or web dependencies."""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from nemsei.jobs.models import ACTOR_SOURCES, Job, JobEvent, ScheduleState, SchedulerLease
from nemsei.shared.clock import as_utc, utc_now


ACTIVE_STATUSES = ("queued", "running", "waiting")
TERMINAL_STATUSES = ("success", "partial", "failed", "cancelled")


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    job_type: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_token: str


def safe_metadata(values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep only small, non-sensitive audit metadata values."""
    # Counters from the abandoned-sync-run sweep. Integers with no provenance:
    # how many runs were looked at and how many were closed. Without them the
    # job's result is `{}` and the only way to know the sweep did anything is
    # to diff `sync_runs` by hand.
    # `called_provider` says whether a deferral cost a real provider call. It
    # is the difference between a `defer_scheduled` and a `defer_no_call`, and
    # without it in the allowlist the event log makes an operator infer from
    # the event type alone which of the two they are looking at.
    # The `source_day`..`batch_persisted` keys are the batch-checkpoint
    # observability a durable intra-day resume needs: which day, how many
    # provider-call batches it takes, how many already landed, and whether
    # this event's own attempt persisted one -- readable from job_events
    # without a database query.
    allowed = {
        "reason", "delay_seconds", "schedule_key", "dedupe_key", "result_status", "mode",
        "next_source_day", "runs_examined", "runs_abandoned", "called_provider",
        "source_day", "mapping_count", "batch_size", "batch_count", "next_batch", "batch_persisted",
    }
    return {
        key: str(value)[:200]
        for key, value in (values or {}).items()
        if key in allowed and value is not None
    }


def _catch_up_slot(slot: datetime, *, now: datetime, interval: timedelta) -> datetime:
    """The slot to fire, skipping a backlog instead of replaying it.

    `next_run_at` advances from its own prior value so the cadence does not
    drift. That is right while the scheduler is running and wrong after it
    stops: on 2026-08-25 the device-status schedule had sat at 2026-08-20 for
    five days, and every tick advanced it by one interval, enqueueing one real
    provider call per tick to work through ~250 missed slots. Eighty cycles and
    165 provider calls went out in five minutes before it was stopped.

    Replaying them was never meaningful. Every one of these schedules reads or
    processes *current* state -- a device poll from five days ago would have
    read exactly the same "now" as the newest one, and a cursor-driven
    production sync does identical work whichever slot triggered it. The
    backlog is duplicate work, not recoverable data.

    Deliberately not applied to digest generation, whose windows chain from the
    previous digest rather than from the slot, and where skipping is therefore
    a different question with a different answer.
    """
    return now if now - slot > interval else slot


def _next_daily_local_time(now: datetime, *, hour: int, minute: int, tz_name: str) -> datetime:
    """The next wall-clock `hour:minute` in `tz_name`, at or after `now`,
    expressed back in UTC -- the morning briefing's 09:00 anchor (reqs
    10-11). A real timezone lookup (`zoneinfo`, already a dependency
    elsewhere in this codebase -- `integrations/fusionsolar/client.py` and
    friends), not raw UTC arithmetic: Europe/Lisbon's offset from UTC
    changes with daylight saving, and "09:00 local" must track the wall
    clock, not a fixed UTC hour that would drift by an hour twice a year.
    """
    zone = ZoneInfo(tz_name)
    local_now = now.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class JobRepository:
    def __init__(self, engine: Engine, session_factory: sessionmaker[Session]) -> None:
        self.engine = engine
        self.session_factory = session_factory

    @contextmanager
    def _immediate_session(self):
        """Short PostgreSQL transaction; handlers never run inside it."""
        with self.session_factory() as session:
            try:
                yield session
                session.flush()
                session.commit()
            except Exception:
                session.rollback()
                raise

    def _event(
        self,
        session: Session,
        *,
        job_id: int,
        event_type: str,
        attempt: int,
        from_status: str | None,
        to_status: str | None,
        actor_source: str,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        if actor_source not in ACTOR_SOURCES:
            raise ValueError("Invalid job event actor source")
        session.add(
            JobEvent(
                job_id=job_id,
                event_type=event_type,
                attempt=attempt,
                from_status=from_status,
                to_status=to_status,
                actor_source=actor_source,
                occurred_at=occurred_at or utc_now(),
                metadata_json=safe_metadata(metadata),
            )
        )

    def enqueue(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        actor_source: str,
        dedupe_key: str | None = None,
        available_at: datetime | None = None,
        priority: int = 100,
        max_attempts: int = 3,
    ) -> tuple[Job, bool]:
        now = utc_now()
        try:
            with self._immediate_session() as session:
                if dedupe_key:
                    existing = session.scalar(
                        select(Job)
                        .where(Job.job_type == job_type, Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUSES))
                        .order_by(Job.id.desc())
                    )
                    if existing is not None:
                        self._event(
                            session,
                            job_id=existing.id,
                            event_type="dedupe_reused",
                            attempt=existing.attempt_count,
                            from_status=existing.status,
                            to_status=existing.status,
                            actor_source=actor_source,
                            metadata={"dedupe_key": dedupe_key},
                            occurred_at=now,
                        )
                        session.expunge(existing)
                        return existing, False
                job = Job(
                    job_type=job_type,
                    status="queued",
                    payload_json=dict(payload),
                    dedupe_key=dedupe_key,
                    priority=priority,
                    available_at=available_at or now,
                    attempt_count=0,
                    max_attempts=max_attempts,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
                session.flush()
                self._event(
                    session,
                    job_id=job.id,
                    event_type="enqueued",
                    attempt=0,
                    from_status=None,
                    to_status="queued",
                    actor_source=actor_source,
                    metadata={"dedupe_key": dedupe_key},
                    occurred_at=now,
                )
                session.expunge(job)
                return job, True
        except IntegrityError:
            # The partial unique index is the final authority. A competing
            # process may commit between an earlier read and this insert.
            if not dedupe_key:
                raise
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job)
                    .where(Job.job_type == job_type, Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUSES))
                    .order_by(Job.id.desc())
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source=actor_source,
                    metadata={"dedupe_key": dedupe_key},
                    occurred_at=now,
                )
                session.expunge(existing)
                return existing, False

    def claim_next(self, *, worker_id: str, lease_seconds: int, now: datetime | None = None) -> ClaimedJob | None:
        now_value = now or utc_now()
        lease_token = secrets.token_urlsafe(24)
        lease_until = now_value + timedelta(seconds=lease_seconds)
        with self.engine.begin() as connection:
            try:
                row = connection.execute(
                    select(
                        Job.id,
                        Job.job_type,
                        Job.payload_json,
                        Job.attempt_count,
                        Job.max_attempts,
                    )
                    .where(Job.status == "queued", Job.available_at <= now_value)
                    .order_by(Job.priority.asc(), Job.available_at.asc(), Job.id.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                ).mappings().first()
                if row is None:
                    return None
                attempt = int(row["attempt_count"]) + 1
                result = connection.execute(
                    update(Job)
                    .where(Job.id == row["id"], Job.status == "queued")
                    .values(
                        status="running",
                        attempt_count=attempt,
                        lease_owner=worker_id,
                        lease_token=lease_token,
                        claimed_at=now_value,
                        lease_expires_at=lease_until,
                        started_at=now_value,
                        updated_at=now_value,
                    )
                )
                if result.rowcount != 1:
                    return None
                connection.execute(
                    insert(JobEvent).values(
                        job_id=row["id"],
                        event_type="claimed",
                        attempt=attempt,
                        from_status="queued",
                        to_status="running",
                        actor_source="worker",
                        occurred_at=now_value,
                        metadata_json={},
                    )
                )
                return ClaimedJob(
                    id=int(row["id"]),
                    job_type=str(row["job_type"]),
                    payload=dict(row["payload_json"] or {}),
                    attempt=attempt,
                    max_attempts=int(row["max_attempts"]),
                    lease_token=lease_token,
                )
            except Exception:
                raise

    def finish(
        self,
        claimed: ClaimedJob,
        *,
        status: str,
        result: dict[str, Any],
        actor_source: str = "worker",
    ) -> bool:
        if status not in {"success", "partial"}:
            raise ValueError("finish only accepts success or partial outcomes")
        now = utc_now()
        with self._immediate_session() as session:
            updated = session.execute(
                update(Job)
                .where(Job.id == claimed.id, Job.status == "running", Job.lease_token == claimed.lease_token)
                .values(
                    status=status,
                    result_json=safe_metadata(result),
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            if updated.rowcount != 1:
                return False
            self._event(
                session,
                job_id=claimed.id,
                event_type="completed",
                attempt=claimed.attempt,
                from_status="running",
                to_status=status,
                actor_source=actor_source,
                metadata={"result_status": status},
                occurred_at=now,
            )
            return True

    def defer(
        self,
        claimed: ClaimedJob,
        *,
        available_at: datetime,
        reason: str,
        max_cycles: int,
        refund_attempt: bool,
        payload: dict[str, Any] | None = None,
        event_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Hold a job until a provider cooldown expires, without spending an attempt.

        This is not `retry_or_fail` with a longer delay. The distinction it
        exists to make is that **nothing was asked of the provider**: the
        request controller refused the call locally against a persisted
        cooldown, so there is no failure to count. Counting it was the whole
        defect -- `claim_next` increments the attempt on every claim, and two
        such refusals plus the one real one exhausted `max_attempts` and failed
        jobs minutes before the cooldown they were waiting on had expired
        (production job 3287 at 16:25:12, device poll 3300 at 16:38:03, both
        2026-08-31).

        So the attempt counter is put back to what it was before this claim,
        the lease is released, and `available_at` is set to when the provider
        said it would answer. No sleep, nothing held.

        **The two kinds of deferral are not the same thing, and the bound only
        applies to one of them.** That was the second defect, and it killed the
        26 replacement backfill jobs 3761-3786 on 2026-09-01:

        * a *paid* deferral cost a real provider call that the provider
          refused. It keeps its attempt, and it counts against `max_cycles` --
          a provider that genuinely refuses over and over must still end the
          job rather than leave it waiting for ever while the schedule
          enqueues its successors.
        * a *free* deferral made no HTTP call at all. It costs the provider
          nothing, holds no lease, and sleeps for nothing; `available_at` is
          pinned to the provider's own cooldown, so it cannot spin. It is
          refunded, it is **never counted**, and it can **never** return False.

        Counting both against one bound meant that past the cap every free
        refusal fell through to `retry_or_fail`, which spent the attempt
        `claim_next` had already taken. Across those 26 jobs, 246 of the 247
        attempt-spending events had `actual_provider_calls = 0`, and 25 of the
        26 terminal `retry_exhausted` events were refusals that never reached
        the provider. Raising `max_attempts` from 3 to 10 only moved the wall.

        Returns False only for a paid deferral that has run out of real
        attempts, so the caller falls back to the ordinary retry path: waiting
        is not the same as never giving up, but only work that was actually
        attempted can be given up on.
        """
        now = utc_now()
        with self._immediate_session() as session:
            if not refund_attempt:
                # A real call was made and refused. Bounded twice over, by the
                # operator ceiling and by the job's own budget of real
                # attempts -- without the second, a generous `max_cycles` would
                # let a job defer past `max_attempts` for ever.
                paid = int(
                    session.scalar(
                        select(func.count())
                        .select_from(JobEvent)
                        .where(JobEvent.job_id == claimed.id, JobEvent.event_type == "defer_scheduled")
                    )
                    or 0
                )
                if paid >= max_cycles or claimed.attempt >= claimed.max_attempts:
                    return False
            event_type = "defer_no_call" if refund_attempt else "defer_scheduled"
            attempt_after = max(0, claimed.attempt - 1) if refund_attempt else claimed.attempt
            values: dict[str, Any] = {
                "status": "waiting",
                # Back to the count before this claim when nothing was
                # asked of the provider. A refusal that cost a real call
                # keeps its attempt -- it was paid for.
                "attempt_count": attempt_after,
                "available_at": available_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
            if payload is not None:
                # Durable progress -- a batch checkpoint, most concretely --
                # that must survive this job past however many more of these
                # cycles it takes for the cooldown to actually lift.
                values["payload_json"] = dict(payload)
            updated = session.execute(
                update(Job)
                .where(Job.id == claimed.id, Job.status == "running", Job.lease_token == claimed.lease_token)
                .values(**values)
            )
            if updated.rowcount != 1:
                return False
            metadata = {"reason": reason, "called_provider": not refund_attempt}
            if event_metadata:
                metadata.update(event_metadata)
            self._event(
                session,
                job_id=claimed.id,
                event_type=event_type,
                attempt=attempt_after,
                from_status="running",
                to_status="waiting",
                actor_source="worker",
                # `called_provider` is what separates the two events, spelled
                # out so an operator reading the log does not have to know
                # which event type means which.
                metadata=metadata,
                occurred_at=now,
            )
            return True

    def retry_or_fail(
        self,
        claimed: ClaimedJob,
        *,
        error_type: str,
        message: str,
        delay_seconds: int = 60,
        payload: dict[str, Any] | None = None,
        event_metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        retryable = claimed.attempt < claimed.max_attempts
        target_status = "waiting" if retryable else "failed"
        values: dict[str, Any] = {
            "status": target_status,
            "error_type": error_type[:120],
            "error_message": message[:2000],
            "updated_at": now,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
        }
        if retryable:
            values["available_at"] = now + timedelta(seconds=delay_seconds)
        else:
            values["finished_at"] = now
        if payload is not None:
            # Durable progress carried forward even on the ordinary retry
            # path -- a batch this attempt landed does not need refetching
            # just because the failure that ended the attempt was not a
            # cooldown.
            values["payload_json"] = dict(payload)
        with self._immediate_session() as session:
            updated = session.execute(
                update(Job)
                .where(Job.id == claimed.id, Job.status == "running", Job.lease_token == claimed.lease_token)
                .values(**values)
            )
            if updated.rowcount != 1:
                return False
            metadata: dict[str, Any] = {"delay_seconds": delay_seconds, "reason": error_type}
            if event_metadata:
                metadata.update(event_metadata)
            self._event(
                session,
                job_id=claimed.id,
                event_type="retry_scheduled" if retryable else "retry_exhausted",
                attempt=claimed.attempt,
                from_status="running",
                to_status=target_status,
                actor_source="worker",
                metadata=metadata,
                occurred_at=now,
            )
            return True

    def reschedule(self, claimed: ClaimedJob, *, payload: dict[str, Any], delay_seconds: int = 0) -> bool:
        """Persist progress before releasing the lease for the next chunk.

        The attempt counter goes back to zero, because a chunk that finished is
        progress, not a retry. `claim_next` increments the counter on every
        claim, so without this a job that needs more than `max_attempts` chunks
        could never finish however well each chunk went: the fourth chunk of a
        three-attempt job is claimed as attempt 4, and one failure there is
        terminal with no retry left. The FusionSolar catch-up needs five
        chunks; a bounded backfill of a year needs twelve.

        This is not an unbounded retry budget. A reset is only ever paid for by
        real, committed progress -- the caller reaches here after a chunk whose
        cursor actually advanced -- so the number of resets is bounded by the
        data remaining, not by how many times the work can fail.
        """
        now = utc_now()
        with self._immediate_session() as session:
            updated = session.execute(
                update(Job)
                .where(Job.id == claimed.id, Job.status == "running", Job.lease_token == claimed.lease_token)
                .values(
                    status="waiting",
                    payload_json=dict(payload),
                    available_at=now + timedelta(seconds=max(0, delay_seconds)),
                    attempt_count=0,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if updated.rowcount != 1:
                return False
            self._event(
                session,
                job_id=claimed.id,
                event_type="progress_saved",
                attempt=claimed.attempt,
                from_status="running",
                to_status="waiting",
                actor_source="worker",
                metadata={"next_source_day": payload.get("next_source_day"), "mode": payload.get("mode")},
                occurred_at=now,
            )
            return True

    def cancel(self, *, job_id: int, actor_source: str, reason: str = "requested") -> str | None:
        """Cancel queued/waiting work or record cooperative cancellation for running work."""
        now = utc_now()
        with self._immediate_session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return None
            if job.status in {"queued", "waiting"}:
                updated = session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == job.status)
                    .values(status="cancelled", finished_at=now, updated_at=now)
                )
                if updated.rowcount != 1:
                    return None
                self._event(
                    session,
                    job_id=job_id,
                    event_type="cancelled",
                    attempt=job.attempt_count,
                    from_status=job.status,
                    to_status="cancelled",
                    actor_source=actor_source,
                    metadata={"reason": reason},
                    occurred_at=now,
                )
                return "cancelled"
            if job.status == "running":
                updated = session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "running", Job.lease_token == job.lease_token)
                    .values(cancellation_requested_at=now, updated_at=now)
                )
                if updated.rowcount != 1:
                    return None
                self._event(
                    session,
                    job_id=job_id,
                    event_type="cancellation_requested",
                    attempt=job.attempt_count,
                    from_status="running",
                    to_status="running",
                    actor_source=actor_source,
                    metadata={"reason": reason},
                    occurred_at=now,
                )
                return "requested"
            return None

    def recover_expired(self, *, now: datetime | None = None) -> int:
        now_value = now or utc_now()
        recovered = 0
        with self._immediate_session() as session:
            stale_jobs = session.execute(
                select(Job.id, Job.attempt_count, Job.max_attempts, Job.lease_token)
                .where(Job.status == "running", Job.lease_expires_at.is_not(None), Job.lease_expires_at <= now_value)
            ).mappings().all()
            for job in stale_jobs:
                target_status = "waiting" if job["attempt_count"] < job["max_attempts"] else "failed"
                values: dict[str, Any] = {
                    "status": target_status,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now_value,
                    "error_type": "LeaseExpired",
                    "error_message": "Worker lease expired before the job completed.",
                }
                if target_status == "waiting":
                    values["available_at"] = now_value
                else:
                    values["finished_at"] = now_value
                updated = session.execute(
                    update(Job)
                    .where(
                        Job.id == job["id"],
                        Job.status == "running",
                        Job.lease_token == job["lease_token"],
                        Job.lease_expires_at <= now_value,
                    )
                    .values(**values)
                )
                if updated.rowcount != 1:
                    continue
                self._event(
                    session,
                    job_id=job["id"],
                    event_type="lease_recovered" if target_status == "waiting" else "retry_exhausted",
                    attempt=job["attempt_count"],
                    from_status="running",
                    to_status=target_status,
                    actor_source="recovery",
                    metadata={"reason": "lease_expired"},
                    occurred_at=now_value,
                )
                recovered += 1
        return recovered

    def activate_due_waiting(self, *, now: datetime | None = None) -> int:
        now_value = now or utc_now()
        activated = 0
        with self._immediate_session() as session:
            waiting_jobs = session.execute(
                select(Job.id, Job.attempt_count).where(Job.status == "waiting", Job.available_at <= now_value)
            ).mappings().all()
            for job in waiting_jobs:
                updated = session.execute(
                    update(Job)
                    .where(Job.id == job["id"], Job.status == "waiting", Job.available_at <= now_value)
                    .values(status="queued", updated_at=now_value)
                )
                if updated.rowcount != 1:
                    continue
                self._event(
                    session,
                    job_id=job["id"],
                    event_type="waiting_activated",
                    attempt=job["attempt_count"],
                    from_status="waiting",
                    to_status="queued",
                    actor_source="recovery",
                    occurred_at=now_value,
                )
                activated += 1
        return activated

    def acquire_scheduler_lease(self, *, owner_token: str, lease_seconds: int, now: datetime | None = None) -> bool:
        now_value = now or utc_now()
        lease_until = now_value + timedelta(seconds=lease_seconds)
        statement = postgresql_insert(SchedulerLease).values(
            name="v2-scheduler",
            owner_token=owner_token,
            lease_expires_at=lease_until,
            updated_at=now_value,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[SchedulerLease.name],
            set_={
                "owner_token": owner_token,
                "lease_expires_at": lease_until,
                "updated_at": now_value,
            },
            where=or_(
                SchedulerLease.lease_expires_at.is_(None),
                SchedulerLease.lease_expires_at <= now_value,
                SchedulerLease.owner_token == owner_token,
            ),
        ).returning(SchedulerLease.name)
        with self.engine.begin() as connection:
            return connection.scalar(statement) is not None

    def enqueue_due_noop(self, *, now: datetime | None = None) -> tuple[Job | None, bool]:
        now_value = now or utc_now()
        key = "system.noop.hourly"
        with self._immediate_session() as session:
            schedule = session.get(ScheduleState, key)
            if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                return None, False
            slot = as_utc(schedule.next_run_at).replace(minute=0, second=0, microsecond=0) if schedule is not None else now_value.replace(minute=0, second=0, microsecond=0)
            dedupe_key = f"{key}:{slot.isoformat()}"
            existing = session.scalar(select(Job).where(Job.job_type == "system.noop", Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUSES)))
            if existing is None:
                job = Job(
                    job_type="system.noop",
                    status="queued",
                    payload_json={"scheduled_for": slot.isoformat()},
                    dedupe_key=dedupe_key,
                    priority=100,
                    available_at=now_value,
                    attempt_count=0,
                    max_attempts=3,
                    created_at=now_value,
                    updated_at=now_value,
                )
                session.add(job)
                session.flush()
                self._event(session, job_id=job.id, event_type="enqueued", attempt=0, from_status=None, to_status="queued", actor_source="scheduler", metadata={"schedule_key": key, "dedupe_key": dedupe_key}, occurred_at=now_value)
                created = True
            else:
                job = existing
                created = False
            if schedule is None:
                schedule = ScheduleState(schedule_key=key, next_run_at=slot, updated_at=now_value)
                session.add(schedule)
            schedule.last_enqueued_at = now_value
            schedule.next_run_at = slot + timedelta(hours=1)
            schedule.updated_at = now_value
            return job, created

    def enqueue_due_device_status_poll(
        self, *, connection_id: int, interval_minutes: int, max_cycles: int | None = None, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one device-status poll job when its persisted schedule is due.

        `max_cycles`, when given, is a **lifetime** hard cap on how many
        `device_status.poll` jobs this schedule (`connection_id`) may ever
        create, counted directly from the `jobs` table -- not a separate
        counter that could drift out of sync with reality or reset on a
        restart. Once the count of jobs ever enqueued for this schedule key
        reaches `max_cycles`, this method stops enqueueing (returns
        `(None, False)`) and stops advancing `next_run_at`, forever, until a
        human raises the cap. This is the structural "hard cap" an
        unattended, restart-safe run needs so it can never run away just
        because nobody is watching it tick.

        M7 Fatia 3 (`docs/v2/DEVICE_TELEMETRY.md`). Same shape as
        `enqueue_due_noop`: a `ScheduleState` row survives a scheduler
        restart without re-deriving cadence from wall-clock alone (restart
        safety), and the dedupe key makes a concurrent or repeated call for
        the same slot inert rather than duplicating work (idempotency).
        Unlike the noop schedule this interval is configurable and not
        hour-aligned -- `next_run_at` advances by exactly `interval_minutes`
        from its own previous value, so drift never compounds across many
        cycles the way re-deriving from `now` on every tick would.

        `connection_id` is a required, explicit parameter, not a loop over
        every enabled FusionSolar connection -- there is no code path here
        that could poll more than the one connection a caller names, which
        is the structural half of "não escalar para a carteira": scaling
        would need a second, deliberate call site, not a config change to
        this one.

        Two concurrent ticks racing for the same due slot (e.g. during a
        scheduler rolling restart) both compute the same `dedupe_key`; the
        loser's insert hits `uq_jobs_active_dedupe` as an `IntegrityError`,
        caught below the same way `enqueue()` already handles it -- rather
        than raising, the loser re-reads and reports the winner's job.
        """
        if interval_minutes <= 0:
            raise ValueError("Device status poll interval must be positive.")
        now_value = now or utc_now()
        key = f"device_status.poll:{connection_id}"
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, key)
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else now_value
                slot = _catch_up_slot(slot, now=now_value, interval=timedelta(minutes=interval_minutes))
                dedupe_key = f"{key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "device_status.poll", Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUSES)
                    )
                )
                if existing is None and max_cycles is not None:
                    lifetime_count = session.scalar(
                        select(func.count(Job.id)).where(
                            Job.job_type == "device_status.poll",
                            Job.dedupe_key.like(f"{key}:%"),
                        )
                    )
                    if lifetime_count >= max_cycles:
                        # Capped: never create the job, never advance the
                        # schedule. `next_run_at` stays exactly where it was,
                        # so this is trivially observable later (the schedule
                        # frozen at a due, never-fired slot) rather than
                        # silently drifting forward as if nothing happened.
                        return None, False
                if existing is None:
                    job = Job(
                        job_type="device_status.poll",
                        status="queued",
                        payload_json={"connection_id": connection_id, "scheduled_for": slot.isoformat()},
                        dedupe_key=dedupe_key,
                        # Lower priority than the default 100 production/report
                        # jobs use -- V1's own real account-wide priority order
                        # (docs/v2/DEVICE_TELEMETRY.md §4) ranks device-level
                        # diagnostics below production, and this mirrors that.
                        priority=150,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(minutes=interval_minutes)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            # A concurrent tick already won this slot -- and, since it
            # committed first, already advanced the schedule too. Nothing is
            # left for this loser to do but report the winner's job.
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "device_status.poll", Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUSES)
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False

    def enqueue_due_production_incremental(
        self, *, connection_id: int, interval_hours: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one `production.incremental` job when its persisted
        schedule is due -- same restart-safe, idempotent, single-connection
        shape as `enqueue_due_device_status_poll` above (read that
        docstring for the full rationale; this one only notes what differs).

        No lifetime hard cap, unlike device-status polling: V1's own daily
        `fusionsolar_production_sync` has none either, and unlike that
        M7 Fatia 3 canary this is meant to run indefinitely once turned on
        for a connection -- capping it would mean silently going stale
        after N days with no signal beyond a frozen `next_run_at`.

        The enqueued payload carries no `start_date`/`end_date` -- an empty
        payload is exactly what `_execute_production` already interprets as
        "resume from wherever `sync_cursors` last got to", the same
        cursor-driven incremental mode this job type always meant, whether
        triggered by a scheduler tick or (as it was for asset 2 today) a
        manual rollout stage.
        """
        if interval_hours <= 0:
            raise ValueError("Production sync interval must be positive.")
        now_value = now or utc_now()
        key = f"production.incremental:{connection_id}"
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, key)
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else now_value
                slot = _catch_up_slot(slot, now=now_value, interval=timedelta(hours=interval_hours))
                dedupe_key = f"{key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "production.incremental", Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUSES)
                    )
                )
                if existing is None:
                    job = Job(
                        job_type="production.incremental",
                        status="queued",
                        payload_json={"connection_id": connection_id, "scheduled_for": slot.isoformat()},
                        dedupe_key=dedupe_key,
                        priority=100,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(hours=interval_hours)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "production.incremental", Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_STATUSES)
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False

    def enqueue_due_incident_evaluation(
        self, *, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one diagnostic-incident evaluation cycle when due.

        D1 (`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`). Same shape as
        `enqueue_due_device_status_poll` (persisted `ScheduleState`, restart
        -safe, idempotent dedupe key) minus the two things that job needs and
        this one does not: a `connection_id` (this evaluates every asset that
        owns a device, not one provider connection) and a lifetime cap (this
        makes zero provider calls -- it only reads already-persisted
        `device_status_facts` and writes `diagnostic_incidents` -- so there is
        no external budget to protect).
        """
        if interval_minutes <= 0:
            raise ValueError("Diagnostic incident evaluation interval must be positive.")
        now_value = now or utc_now()
        key = "diagnostics.evaluate_incidents"
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, key)
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else now_value
                slot = _catch_up_slot(slot, now=now_value, interval=timedelta(minutes=interval_minutes))
                dedupe_key = f"{key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "diagnostics.evaluate_incidents",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    job = Job(
                        job_type="diagnostics.evaluate_incidents",
                        status="queued",
                        payload_json={"scheduled_for": slot.isoformat()},
                        dedupe_key=dedupe_key,
                        # Same tier as device_status.poll: diagnostics ranks
                        # below production/report jobs.
                        priority=150,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(minutes=interval_minutes)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "diagnostics.evaluate_incidents",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False


    def enqueue_due_sync_run_sweep(
        self, *, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one abandoned-sync-run sweep when due.

        Same shape as `enqueue_due_incident_evaluation` above: a persisted
        `ScheduleState`, restart-safe, idempotent dedupe key, no connection id
        and no lifetime cap, because this makes zero provider calls -- it reads
        `sync_runs` and closes the ones whose owner is provably gone.

        On its own schedule rather than inside the worker's per-cycle recovery
        pass, because it is not free: it scans every running run and asks each
        one's owner-liveness resolvers. Every fifteen minutes is far more often
        than a process dies, and a schedule makes the sweep visible on the
        automations screen like every other automation, instead of being an
        invisible side effect of polling.
        """
        if interval_minutes <= 0:
            raise ValueError("Sync run sweep interval must be positive.")
        now_value = now or utc_now()
        key = "sync_runs.sweep_abandoned"
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, key)
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else now_value
                slot = _catch_up_slot(slot, now=now_value, interval=timedelta(minutes=interval_minutes))
                dedupe_key = f"{key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "sync_runs.sweep_abandoned",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    job = Job(
                        job_type="sync_runs.sweep_abandoned",
                        status="queued",
                        payload_json={"scheduled_for": slot.isoformat()},
                        dedupe_key=dedupe_key,
                        # Same tier as device_status.poll: diagnostics ranks
                        # below production/report jobs.
                        priority=150,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(minutes=interval_minutes)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "sync_runs.sweep_abandoned",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False

    def enqueue_due_notification_processing(
        self, *, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one notification-processing cycle when due.

        D3 (`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`). Same shape as
        `enqueue_due_incident_evaluation` -- no connection id, no cap, for the
        same reason: this reads `diagnostic_incidents` and writes
        `notification_events`, and even its "delivery" step can only ever
        reach the mock Telegram client in this codebase (D4 has not
        happened), so there is no external budget to protect here either.
        """
        if interval_minutes <= 0:
            raise ValueError("Notification processing interval must be positive.")
        now_value = now or utc_now()
        key = "notifications.process"
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, key)
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else now_value
                slot = _catch_up_slot(slot, now=now_value, interval=timedelta(minutes=interval_minutes))
                dedupe_key = f"{key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "notifications.process",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    job = Job(
                        job_type="notifications.process",
                        status="queued",
                        payload_json={"scheduled_for": slot.isoformat()},
                        dedupe_key=dedupe_key,
                        priority=150,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(minutes=interval_minutes)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "notifications.process",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False

    def enqueue_due_digest_generation(
        self, *, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one digest-generation cycle when due.

        D6 (`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`). Same shape as
        `enqueue_due_notification_processing` -- the `scheduled_for` value
        stashed in `payload_json` here is what
        `notifications.digests.generate_digest` uses as the digest's
        `window_end`, not a fresh `now()` read at execution time. That is
        what makes two concurrent attempts for the *same due slot* compute
        the identical window, so `DigestRun`'s own unique
        `(window_start, window_end)` constraint actually catches a real
        race instead of two almost-but-not-quite-equal timestamps that
        would never collide.
        """
        if interval_minutes <= 0:
            raise ValueError("Digest generation interval must be positive.")
        now_value = now or utc_now()
        key = "digests.generate"
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, key)
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else now_value
                dedupe_key = f"{key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "digests.generate",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    job = Job(
                        job_type="digests.generate",
                        status="queued",
                        payload_json={"scheduled_for": slot.isoformat()},
                        dedupe_key=dedupe_key,
                        priority=150,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(minutes=interval_minutes)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "digests.generate",
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False

    def enqueue_due_recovery_digest(
        self, *, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one recovery-digest cycle when due -- req 13's grouped
        recoveries. Same shape as `enqueue_due_digest_generation` (D6): both
        are content summaries whose windows chain from the previous digest
        of the same kind, not from the schedule slot, so a missed cycle
        after downtime is a genuinely different (wider) window to summarise
        -- not duplicate work `_catch_up_slot` should skip. Deliberately its
        own small method, not `_enqueue_due_cycle`, for exactly that reason:
        `_enqueue_due_cycle` always applies `_catch_up_slot`, which is right
        for a state poll and wrong for a digest.
        """
        return self._enqueue_due_digest(
            schedule_key="digests.generate.recoveries", kind="recoveries",
            interval_minutes=interval_minutes, now=now,
        )

    def enqueue_due_morning_briefing(
        self, *, interval_minutes: int = 1440, hour: int = 9, minute: int = 0,
        tz_name: str = "Europe/Lisbon", now: datetime | None = None,
    ) -> tuple[Job | None, bool]:
        """Enqueue one morning-briefing cycle when due -- reqs 10-11.

        Unlike the two digest kinds above, this is a point-in-time snapshot
        of the fleet, not a window aggregation (see
        `notifications/digests.py::build_morning_briefing_payload`), so it
        uses `_enqueue_due_cycle` -- catch-up-by-skipping is the *right*
        behaviour here: three backlogged briefings after an outage would all
        show roughly the same growing/shrinking snapshot at different stale
        timestamps, not three genuinely different summaries, so skipping to
        one current briefing (same reasoning `_catch_up_slot`'s own
        docstring gives for a device poll) is correct, not a compromise.

        The very first activation anchors to the *next* 09:00 in `tz_name`
        (`_next_daily_local_time`), not "now" -- turning this on at 14:00
        must not fire a briefing mid-afternoon.
        """
        now_value = now or utc_now()
        return self._enqueue_due_cycle(
            job_type="digests.generate",
            schedule_key="digests.generate.morning_briefing",
            interval_minutes=interval_minutes,
            payload={"kind": "morning_briefing"},
            initial_slot=_next_daily_local_time(now_value, hour=hour, minute=minute, tz_name=tz_name),
            now=now_value,
        )

    def _enqueue_due_digest(
        self, *, schedule_key: str, kind: str, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """The chaining-without-catch-up shape `enqueue_due_digest_generation`
        established for D6 -- extracted here for `enqueue_due_recovery_digest`
        to reuse rather than duplicate, without touching that pre-existing,
        tested method's own inline copy of the same logic (the Huawei SCADA
        precedent: retrofitting old, working schedules onto a new shared
        helper is a risk this codebase has already decided, twice, not to
        take for no operational gain).
        """
        if interval_minutes <= 0:
            raise ValueError(f"{schedule_key} interval must be positive.")
        now_value = now or utc_now()
        job_type = "digests.generate"
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, schedule_key)
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else now_value
                dedupe_key = f"{schedule_key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == job_type,
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    job = Job(
                        job_type=job_type,
                        status="queued",
                        payload_json={"scheduled_for": slot.isoformat(), "kind": kind},
                        dedupe_key=dedupe_key,
                        priority=150,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": schedule_key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=schedule_key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(minutes=interval_minutes)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == job_type,
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": schedule_key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False

    def _enqueue_due_cycle(
        self,
        *,
        job_type: str,
        schedule_key: str,
        interval_minutes: int,
        payload: dict[str, Any] | None = None,
        priority: int = 150,
        initial_slot: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[Job | None, bool]:
        """One due-slot enqueue, shared by the schedules that need nothing else.

        The four `enqueue_due_*` methods above each grew their own copy of this
        logic. The Huawei SCADA schedules deliberately do not add a fifth and a
        sixth: they take neither a provider call budget nor a lifetime cap --
        the rollup integrates rows already in the database and the retention
        pass only deletes them -- so there is nothing left to specialise.

        Same guarantees as the copies: a persisted `ScheduleState` so a
        restart resumes rather than replays, `_catch_up_slot` so a scheduler
        stopped for days enqueues one cycle instead of the whole backlog, and
        a dedupe key that makes two concurrent ticks idempotent (with the
        `IntegrityError` fallback for the race the unique index catches).

        `initial_slot` overrides only the very first slot, for a schedule
        whose first due moment is not simply "whenever this got turned on" --
        the morning briefing's next-09:00 anchor
        (`_next_daily_local_time`/`enqueue_due_morning_briefing`), so
        activating it at 14:00 does not fire a briefing mid-afternoon. Every
        slot after the first still advances by `interval_minutes` and is
        still subject to `_catch_up_slot` exactly like any other schedule.
        """
        if interval_minutes <= 0:
            raise ValueError(f"{job_type} interval must be positive.")
        now_value = now or utc_now()
        dedupe_key = ""
        try:
            with self._immediate_session() as session:
                schedule = session.get(ScheduleState, schedule_key)
                if schedule is None and initial_slot is not None and initial_slot > now_value:
                    # First activation, anchored to a future slot (the next
                    # 09:00 local, not "now"). Persist the schedule so it is
                    # not recomputed -- and does not drift -- on the next
                    # tick, but enqueue nothing until that slot actually
                    # arrives: without this branch, the code below would
                    # fall through and enqueue a job right now carrying a
                    # future `scheduled_for`, which is exactly the
                    # mid-afternoon briefing this parameter exists to
                    # prevent.
                    session.add(ScheduleState(schedule_key=schedule_key, next_run_at=initial_slot, updated_at=now_value))
                    session.flush()
                    return None, False
                if schedule is not None and as_utc(schedule.next_run_at) > now_value:
                    return None, False
                slot = as_utc(schedule.next_run_at) if schedule is not None else (initial_slot or now_value)
                slot = _catch_up_slot(slot, now=now_value, interval=timedelta(minutes=interval_minutes))
                dedupe_key = f"{schedule_key}:{slot.isoformat()}"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == job_type,
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    job = Job(
                        job_type=job_type,
                        status="queued",
                        payload_json={"scheduled_for": slot.isoformat(), **(payload or {})},
                        dedupe_key=dedupe_key,
                        priority=priority,
                        available_at=now_value,
                        attempt_count=0,
                        max_attempts=3,
                        created_at=now_value,
                        updated_at=now_value,
                    )
                    session.add(job)
                    session.flush()
                    self._event(
                        session,
                        job_id=job.id,
                        event_type="enqueued",
                        attempt=0,
                        from_status=None,
                        to_status="queued",
                        actor_source="scheduler",
                        metadata={"schedule_key": schedule_key, "dedupe_key": dedupe_key},
                        occurred_at=now_value,
                    )
                    created = True
                else:
                    job = existing
                    created = False
                if schedule is None:
                    schedule = ScheduleState(schedule_key=schedule_key, next_run_at=slot, updated_at=now_value)
                    session.add(schedule)
                schedule.last_enqueued_at = now_value
                schedule.next_run_at = slot + timedelta(minutes=interval_minutes)
                schedule.updated_at = now_value
                session.flush()
                session.expunge(job)
                return job, created
        except IntegrityError:
            with self._immediate_session() as session:
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == job_type,
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
                if existing is None:
                    raise
                self._event(
                    session,
                    job_id=existing.id,
                    event_type="dedupe_reused",
                    attempt=existing.attempt_count,
                    from_status=existing.status,
                    to_status=existing.status,
                    actor_source="scheduler",
                    metadata={"schedule_key": schedule_key, "dedupe_key": dedupe_key},
                    occurred_at=now_value,
                )
                session.expunge(existing)
                return existing, False

    def enqueue_due_report_month_close(
        self, *, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one report-finalisation cycle when due. No provider call.

        Same shape as the incident evaluator and for the same reasons: it runs
        across every asset that has a provisional month rather than against one
        provider connection, and it reads persisted facts only, so there is no
        external budget to protect and no lifetime cap to spend.
        """
        return self._enqueue_due_cycle(
            job_type="reporting.month_close",
            schedule_key="reporting.month_close",
            interval_minutes=interval_minutes,
            now=now,
        )

    def enqueue_due_huawei_scada_rollup(
        self, *, connection_id: int, interval_minutes: int, lookback_days: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one power-to-energy rollup cycle when due. No provider call."""
        return self._enqueue_due_cycle(
            job_type="huawei_scada.rollup",
            schedule_key=f"huawei_scada.rollup:{connection_id}",
            interval_minutes=interval_minutes,
            payload={"connection_id": connection_id, "lookback_days": lookback_days},
            now=now,
        )

    def enqueue_due_huawei_scada_retention(
        self, *, connection_id: int, interval_minutes: int, retention_days: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one sample-retention pass when due. Deletes, never reads out."""
        return self._enqueue_due_cycle(
            job_type="huawei_scada.retention",
            schedule_key=f"huawei_scada.retention:{connection_id}",
            interval_minutes=interval_minutes,
            payload={"connection_id": connection_id, "retention_days": retention_days},
            # Below everything else: reclaiming disk can always wait for a
            # report or a sync to finish first.
            priority=200,
            now=now,
        )

    def enqueue_due_current_monitoring(
        self, *, connection_id: int, interval_minutes: int, now: datetime | None = None
    ) -> tuple[Job | None, bool]:
        """Enqueue one plant-state read when due.

        The cheapest read V2 makes per installation covered: FusionSolar
        answers up to 100 plants in a single batched call, so the whole
        mapped fleet costs two calls plus a cached login. It also has its own
        `current_monitoring` endpoint family in `provider_request_states`,
        separate from the device-level lanes that carry today's rate-limit
        pressure, so this does not compete with the device poll's budget.
        """
        return self._enqueue_due_cycle(
            job_type="monitoring.current",
            schedule_key=f"monitoring.current:{connection_id}",
            interval_minutes=interval_minutes,
            payload={"connection_id": connection_id},
            # Above device diagnostics (150) and below production (100).
            # "Is this plant up" is the question the alerting depends on, and
            # answering it costs one call -- it should not queue behind a
            # per-device sweep of a single station.
            priority=120,
            now=now,
        )

    def events_for(self, job_id: int) -> list[JobEvent]:
        with self.session_factory() as session:
            return list(session.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)))
