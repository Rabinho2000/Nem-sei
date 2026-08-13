"""SQLite-backed persisted queue operations; no handler or web dependencies."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, insert, select, update
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
    allowed = {"reason", "delay_seconds", "schedule_key", "dedupe_key", "result_status"}
    return {
        key: str(value)[:200]
        for key, value in (values or {}).items()
        if key in allowed and value is not None
    }


class JobRepository:
    def __init__(self, engine: Engine, session_factory: sessionmaker[Session]) -> None:
        self.engine = engine
        self.session_factory = session_factory

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
        with self.session_factory.begin() as session:
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
            return job, True

    def claim_next(self, *, worker_id: str, lease_seconds: int, now: datetime | None = None) -> ClaimedJob | None:
        now_value = now or utc_now()
        lease_token = secrets.token_urlsafe(24)
        lease_until = now_value + timedelta(seconds=lease_seconds)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
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
                ).mappings().first()
                if row is None:
                    connection.commit()
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
                    connection.rollback()
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
                connection.commit()
                return ClaimedJob(
                    id=int(row["id"]),
                    job_type=str(row["job_type"]),
                    payload=dict(row["payload_json"] or {}),
                    attempt=attempt,
                    max_attempts=int(row["max_attempts"]),
                    lease_token=lease_token,
                )
            except Exception:
                connection.rollback()
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
        with self.session_factory.begin() as session:
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
        with self.session_factory.begin() as session:
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

    def recover_expired(self, *, now: datetime | None = None) -> int:
        now_value = now or utc_now()
        recovered = 0
        with self.session_factory.begin() as session:
            stale_jobs = session.scalars(
                select(Job).where(Job.status == "running", Job.lease_expires_at.is_not(None), Job.lease_expires_at <= now_value)
            ).all()
            for job in stale_jobs:
                target_status = "waiting" if job.attempt_count < job.max_attempts else "failed"
                job.status = target_status
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.updated_at = now_value
                job.error_type = "LeaseExpired"
                job.error_message = "Worker lease expired before the job completed."
                if target_status == "waiting":
                    job.available_at = now_value
                else:
                    job.finished_at = now_value
                self._event(
                    session,
                    job_id=job.id,
                    event_type="lease_recovered" if target_status == "waiting" else "retry_exhausted",
                    attempt=job.attempt_count,
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
        with self.session_factory.begin() as session:
            waiting_jobs = session.scalars(
                select(Job).where(Job.status == "waiting", Job.available_at <= now_value)
            ).all()
            for job in waiting_jobs:
                job.status = "queued"
                job.updated_at = now_value
                self._event(
                    session,
                    job_id=job.id,
                    event_type="waiting_activated",
                    attempt=job.attempt_count,
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
        with self.session_factory.begin() as session:
            lease = session.get(SchedulerLease, "v2-scheduler")
            if lease is not None and lease.lease_expires_at and as_utc(lease.lease_expires_at) > now_value and lease.owner_token != owner_token:
                return False
            if lease is None:
                lease = SchedulerLease(name="v2-scheduler", updated_at=now_value)
                session.add(lease)
            lease.owner_token = owner_token
            lease.lease_expires_at = lease_until
            lease.updated_at = now_value
            return True

    def enqueue_due_noop(self, *, now: datetime | None = None) -> tuple[Job | None, bool]:
        now_value = now or utc_now()
        key = "system.noop.hourly"
        with self.session_factory.begin() as session:
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

    def events_for(self, job_id: int) -> list[JobEvent]:
        with self.session_factory() as session:
            return list(session.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)))
