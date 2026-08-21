from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.diagnostics.incidents import evaluate_and_persist_incidents
from nemsei.integrations.fusionsolar.device_status import FusionSolarDeviceStatusService
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.jobs.repository import ClaimedJob
from nemsei.notifications.digests import deliver_digest, generate_digest
from nemsei.notifications.service import evaluate_and_process_notifications
from nemsei.system.noop_service import execute_noop


@dataclass(frozen=True)
class JobOutcome:
    status: str
    result: dict[str, Any]
    resume_payload: dict[str, Any] | None = None


class RetryableJobError(RuntimeError):
    pass


def execute(
    job: ClaimedJob,
    *,
    testing: bool,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> JobOutcome:
    if job.job_type == "system.noop":
        return JobOutcome(status="success", result=execute_noop(job.payload, testing=testing))
    if job.job_type in {"production.incremental", "production.reconciliation", "production.bounded_backfill"}:
        if settings is None or session_factory is None:
            raise ValueError("Production jobs require worker settings and sessions.")
        return _execute_production(job, settings=settings, session_factory=session_factory)
    if job.job_type == "device_status.poll":
        if settings is None or session_factory is None:
            raise ValueError("Device status jobs require worker settings and sessions.")
        return _execute_device_status_poll(job, settings=settings, session_factory=session_factory)
    if job.job_type == "diagnostics.evaluate_incidents":
        if session_factory is None:
            raise ValueError("Diagnostic incident evaluation requires a worker session factory.")
        return _execute_incident_evaluation(session_factory=session_factory)
    if job.job_type == "notifications.process":
        if session_factory is None:
            raise ValueError("Notification processing requires a worker session factory.")
        return _execute_notification_processing(session_factory=session_factory)
    if job.job_type == "digests.generate":
        if settings is None or session_factory is None:
            raise ValueError("Digest generation requires worker settings and a session factory.")
        return _execute_digest_generation(job, settings=settings, session_factory=session_factory)
    raise ValueError(f"Unsupported V2 foundation job type: {job.job_type}")


def _date(payload: dict[str, Any], key: str, *, required: bool = False) -> date | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Production job requires ISO {key}.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Production job {key} is invalid.") from exc


def _execute_production(job: ClaimedJob, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    connection_id = job.payload.get("connection_id")
    if not isinstance(connection_id, int) or connection_id <= 0:
        raise ValueError("Production job connection_id is invalid.")
    service = FusionSolarProductionService(session_factory, settings)
    if job.job_type == "production.incremental":
        result = service.sync_incremental(connection_id, start_date=_date(job.payload, "start_date"), end_date=_date(job.payload, "end_date"))
    elif job.job_type == "production.reconciliation":
        days = job.payload.get("source_days", 1)
        if not isinstance(days, int):
            raise ValueError("Production reconciliation source_days is invalid.")
        result = service.sync_reconciliation(connection_id, source_days=days)
    else:
        result = service.sync_bounded_backfill(
            connection_id,
            start_date=_date(job.payload, "start_date", required=True),
            end_date=_date(job.payload, "end_date", required=True),
            resume_from=_date(job.payload, "next_source_day"),
        )
    result_json = {"mode": result.mode, "result_status": result.status}
    if result.status in {"failed", "rate_limited", "deferred", "partial"}:
        raise RetryableJobError(f"Production {result.mode} stopped with {result.status}.")
    if result.next_source_day:
        payload = dict(job.payload)
        payload["next_source_day"] = result.next_source_day.isoformat()
        payload["mode"] = result.mode
        return JobOutcome(status="success", result=result_json, resume_payload=payload)
    return JobOutcome(status="success", result=result_json)


def _execute_device_status_poll(job: ClaimedJob, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    """One device-status poll cycle. Never writes an "offline" fact on failure.

    `FusionSolarDeviceStatusService.sync_device_status` only ever calls
    `record_device_status` for a device it actually read successfully
    (`device_status.py`); a device this cycle failed to read simply gets no
    new fact, so `current_device_status()` keeps reporting its last known
    good reading. A raised `RetryableJobError` here only stops *this job*
    from being marked done -- it never touches `device_status_facts`, so it
    cannot erase or downgrade evidence a previous successful cycle wrote.
    """
    connection_id = job.payload.get("connection_id")
    if not isinstance(connection_id, int) or connection_id <= 0:
        raise ValueError("Device status poll job connection_id is invalid.")
    service = FusionSolarDeviceStatusService(session_factory, settings)
    result = service.sync_device_status(connection_id)
    result_json = {
        "result_status": result.status,
        "expected": result.expected,
        "received": result.received,
        "accepted": result.accepted,
        "rejected": result.rejected,
    }
    if result.status in {"failed", "rate_limited", "deferred", "partial"}:
        raise RetryableJobError(f"Device status poll stopped with {result.status}.")
    return JobOutcome(status="success", result=result_json)


def _execute_incident_evaluation(*, session_factory: sessionmaker[Session]) -> JobOutcome:
    """One diagnostic-incident evaluation pass. No provider calls, no retry logic
    needed beyond the worker's own generic retry: a failure here (e.g. a
    transient DB error) never writes a partial row, because
    `evaluate_and_persist_incidents` flushes per asset inside the caller's
    transaction and the whole job either commits or rolls back as one unit.
    """
    with session_factory() as session, session.begin():
        summary = evaluate_and_persist_incidents(session)
    return JobOutcome(
        status="success",
        result={
            "assets_evaluated": summary.assets_evaluated,
            "incidents_opened": summary.incidents_opened,
            "incidents_confirmed": summary.incidents_confirmed,
            "incidents_resolved": summary.incidents_resolved,
        },
    )


def _execute_notification_processing(*, session_factory: sessionmaker[Session]) -> JobOutcome:
    """One notification-policy evaluation + mock-delivery pass (D3).

    No provider calls. Delivery can only ever reach
    `notifications.telegram_client.MockTelegramClient` -- `service.py`'s
    default client factory has no other client to construct. A delivery
    "failure" here is the mock's own, deterministic, test-configured
    behaviour, never a real network error, since D4 has not happened.
    """
    # No outer `session.begin()` here, deliberately -- `evaluate_and_process
    # _notifications` manages its own transactions per step (one for
    # deciding, one per event for delivering), which is what makes a crash
    # mid-delivery unable to resend an already-committed, already-sent
    # message on the next retry. See notifications/service.py's docstring.
    summary = evaluate_and_process_notifications(session_factory)
    return JobOutcome(
        status="success",
        result={
            "policies_evaluated": summary.policies_evaluated,
            "events_created": summary.events_created,
            "events_skipped": summary.events_skipped,
            "delivery_sent": summary.delivery_sent,
            "delivery_failed": summary.delivery_failed,
        },
    )


def _execute_digest_generation(job: ClaimedJob, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    """One digest-generation pass, then a delivery attempt (D6).

    `window_end` comes from the job's own `scheduled_for` payload -- the
    same due-slot value `JobRepository.enqueue_due_digest_generation`
    persisted -- never a fresh `datetime.now()` read here, which is what
    makes `DigestRun`'s unique `(window_start, window_end)` constraint
    actually catch a concurrent-retry race. No provider call anywhere in
    this path; delivery can only ever reach the mock Telegram client.
    """
    scheduled_for = job.payload.get("scheduled_for")
    if not isinstance(scheduled_for, str):
        raise ValueError("Digest generation job is missing its scheduled_for window end.")
    window_end = datetime.fromisoformat(scheduled_for)

    with session_factory() as session, session.begin():
        digest = generate_digest(session, window_end=window_end, interval_minutes=settings.digest_generation_interval_minutes)
        digest_id = digest.id if digest is not None else None

    delivery_attempted = False
    if digest_id is not None:
        result = deliver_digest(session_factory, digest_run_id=digest_id)
        delivery_attempted = result.attempted

    return JobOutcome(
        status="success",
        result={
            "digest_generated": digest_id is not None,
            "digest_id": digest_id,
            "delivery_attempted": delivery_attempted,
        },
    )
