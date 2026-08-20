"""PostgreSQL-backed persisted queue operations; no handler or web dependencies."""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

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
    allowed = {"reason", "delay_seconds", "schedule_key", "dedupe_key", "result_status", "mode", "next_source_day"}
    return {
        key: str(value)[:200]
        for key, value in (values or {}).items()
        if key in allowed and value is not None
    }


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

    def retry_or_fail(self, claimed: ClaimedJob, *, error_type: str, message: str, delay_seconds: int = 60) -> bool:
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
        with self._immediate_session() as session:
            updated = session.execute(
                update(Job)
                .where(Job.id == claimed.id, Job.status == "running", Job.lease_token == claimed.lease_token)
                .values(**values)
            )
            if updated.rowcount != 1:
                return False
            self._event(
                session,
                job_id=claimed.id,
                event_type="retry_scheduled" if retryable else "retry_exhausted",
                attempt=claimed.attempt,
                from_status="running",
                to_status=target_status,
                actor_source="worker",
                metadata={"delay_seconds": delay_seconds, "reason": error_type},
                occurred_at=now,
            )
            return True

    def reschedule(self, claimed: ClaimedJob, *, payload: dict[str, Any], delay_seconds: int = 0) -> bool:
        """Persist backfill progress before releasing the lease for the next chunk."""
        now = utc_now()
        with self._immediate_session() as session:
            updated = session.execute(
                update(Job)
                .where(Job.id == claimed.id, Job.status == "running", Job.lease_token == claimed.lease_token)
                .values(
                    status="waiting",
                    payload_json=dict(payload),
                    available_at=now + timedelta(seconds=max(0, delay_seconds)),
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

    def events_for(self, job_id: int) -> list[JobEvent]:
        with self.session_factory() as session:
            return list(session.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)))
