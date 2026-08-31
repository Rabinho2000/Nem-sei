from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.diagnostics.incidents import evaluate_and_persist_incidents
from nemsei.integrations.fusionsolar.device_status import FusionSolarDeviceStatusService
from nemsei.integrations.fusionsolar.monitoring import FusionSolarMonitoringService
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.integrations.huawei_scada.retention import purge_samples
from nemsei.integrations.huawei_scada.rollup import HuaweiScadaRollupService
from nemsei.integrations.sigenergy.monitoring import SigenergyMonitoringService
from nemsei.integrations.sigenergy.production import SigenergyProductionService
from nemsei.providers.models import ProviderConnection
from nemsei.providers.registry import ProviderCode
from nemsei.integrations.fusionsolar.session_cache import default_session_cache
from nemsei.jobs.repository import ClaimedJob
from nemsei.notifications.digests import deliver_digest, generate_digest
from nemsei.notifications.service import evaluate_and_process_notifications
from nemsei.system.noop_service import execute_noop


@dataclass(frozen=True)
class JobOutcome:
    status: str
    result: dict[str, Any]
    resume_payload: dict[str, Any] | None = None
    # How long to wait before the next chunk. Zero -- the old, only behaviour --
    # means a chunked job resumes immediately, which turns "one small window per
    # run" back into one long burst against the provider.
    resume_delay_seconds: int = 0


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
    if job.job_type == "monitoring.current":
        if settings is None or session_factory is None:
            raise ValueError("Plant state jobs require worker settings and sessions.")
        return _execute_current_monitoring(job, settings=settings, session_factory=session_factory)
    if job.job_type == "diagnostics.evaluate_incidents":
        if session_factory is None:
            raise ValueError("Diagnostic incident evaluation requires a worker session factory.")
        return _execute_incident_evaluation(session_factory=session_factory)
    if job.job_type == "notifications.process":
        if session_factory is None:
            raise ValueError("Notification processing requires a worker session factory.")
        return _execute_notification_processing(session_factory=session_factory)
    if job.job_type in {"huawei_scada.rollup", "huawei_scada.retention"}:
        if settings is None or session_factory is None:
            raise ValueError("Huawei SCADA jobs require worker settings and sessions.")
        return _execute_huawei_scada(job, settings=settings, session_factory=session_factory)
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


def _execute_sigenergy_production(job: ClaimedJob, connection_id: int, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    """Sigenergy has no reconciliation or backfill modes yet; only incremental.

    Refusing the other two loudly is better than quietly treating them as
    incremental, which would make a backfill request look like it succeeded
    while covering a completely different window.
    """
    if job.job_type != "production.incremental":
        raise ValueError(f"Sigenergy production supports only production.incremental, not {job.job_type}.")
    result = SigenergyProductionService(session_factory, settings).sync_incremental(connection_id)
    return JobOutcome(
        status="success" if result.status in ("success", "partial") else "failed",
        result={"mode": "daily_history", "result_status": result.status, "facts_written": result.facts_written},
    )


def _execute_current_monitoring(job: ClaimedJob, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    """One plant-state read for one connection, dispatched by its provider.

    Dispatching on `provider_code` from the start, rather than growing a
    FusionSolar-only path first: that was exactly the bug production had
    (commit 094a40a), where a Sigenergy job could only ever be answered with
    "Connection is not FusionSolar".

    A rate-limited or failed read is **not** a failed job. The provider
    refusing to answer is a known operating condition of a shared account,
    the services already record it on the sync run and the connection's
    health, and marking the job failed would retry it three times against an
    account that just said no. What must never happen -- and cannot, because
    neither service writes an observation on error -- is a failed read
    becoming an "offline" plant.
    """
    connection_id = job.payload.get("connection_id")
    if not isinstance(connection_id, int) or connection_id <= 0:
        raise ValueError("Plant state job connection_id is invalid.")
    with session_factory() as session:
        connection = session.get(ProviderConnection, connection_id)
        provider_code = connection.provider_code if connection else None
    if provider_code == ProviderCode.SIGENERGY.value:
        result = SigenergyMonitoringService(session_factory, settings).sync_current_monitoring(connection_id)
    elif provider_code == ProviderCode.FUSIONSOLAR.value:
        result = FusionSolarMonitoringService(session_factory, settings, session_cache=default_session_cache()).sync_current_monitoring(connection_id)
    else:
        raise ValueError(f"Plant state is not supported for provider {provider_code!r}.")
    return JobOutcome(
        status="success",
        result={
            "result_status": result.status,
            "expected": result.expected,
            "accepted": result.accepted,
            "rejected": result.rejected,
            "error_code": result.error_code,
        },
    )


def _execute_production(job: ClaimedJob, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    connection_id = job.payload.get("connection_id")
    if not isinstance(connection_id, int) or connection_id <= 0:
        raise ValueError("Production job connection_id is invalid.")
    # Shared, process-wide session cache: this worker's other production and
    # device-status jobs reuse the same FusionSolar login instead of each
    # authenticating from scratch (session_cache.py; see
    # docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md's rollout write-up for why --
    # 13 logins from a handful of test syncs tripped the provider's own
    # login rate limit before this existed).
    # Which provider this connection belongs to decides the service. Before
    # this the FusionSolar service was built unconditionally, so a production
    # job for any other provider was answered with "Connection is not
    # FusionSolar" -- the Sigenergy service could never have run at all.
    with session_factory() as session:
        connection = session.get(ProviderConnection, connection_id)
        provider_code = connection.provider_code if connection else None
    if provider_code == ProviderCode.SIGENERGY.value:
        return _execute_sigenergy_production(job, connection_id, settings=settings, session_factory=session_factory)

    service = FusionSolarProductionService(session_factory, settings, session_cache=default_session_cache())
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
        # An incremental chunk resumes from the cursor it just advanced, so the
        # payload's `next_source_day` is a record of where it got to rather
        # than an instruction; a bounded backfill reads it back as its resume
        # point. Both wait: the pause between chunks is the whole reason
        # chunking protects the account.
        return JobOutcome(
            status="success",
            result=result_json,
            resume_payload=payload,
            resume_delay_seconds=settings.production_incremental_chunk_pause_seconds,
        )
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
    service = FusionSolarDeviceStatusService(session_factory, settings, session_cache=default_session_cache())
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


def _execute_huawei_scada(job: ClaimedJob, *, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    """Rollup or retention for one Huawei SCADA connection. Zero provider calls.

    Both jobs read and write rows this deployment already holds: the listener
    is the only thing that ever talks to a dongle, and it is a separate
    process that no job can start. A failure here therefore cannot cost a
    provider call, and cannot lose a sample either -- the rollup only writes
    facts, and retention only deletes samples whose day is already final.
    """
    connection_id = job.payload.get("connection_id")
    if not isinstance(connection_id, int) or connection_id <= 0:
        raise ValueError("Huawei SCADA job connection_id is invalid.")
    if job.job_type == "huawei_scada.retention":
        days = job.payload.get("retention_days", settings.huawei_scada_retention_days)
        if not isinstance(days, int) or days <= 0:
            raise ValueError("Huawei SCADA retention_days is invalid.")
        purged = purge_samples(session_factory, connection_id=connection_id, retention_days=days)
        return JobOutcome(
            status="success",
            result={
                "mode": "retention",
                "days_deleted": purged.days_deleted,
                "samples_deleted": purged.samples_deleted,
                "mappings_examined": purged.mappings_examined,
                "skipped": purged.skipped_reasons,
            },
        )

    lookback = job.payload.get("lookback_days", settings.huawei_scada_rollup_lookback_days)
    if not isinstance(lookback, int) or lookback <= 0:
        raise ValueError("Huawei SCADA rollup lookback_days is invalid.")
    result = HuaweiScadaRollupService(session_factory, settings).roll_up(connection_id, lookback_days=lookback)
    return JobOutcome(
        status="success",
        result={
            "mode": "power_integral_rollup",
            "days_requested": result.days_requested,
            "days_with_samples": result.days_with_samples,
            "facts_written": result.facts_written,
            "mappings_selected": result.mappings_selected,
            "mappings_skipped": result.mappings_skipped,
            "skipped": result.skipped_reasons,
            "warnings": result.warnings[:20],
        },
    )


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
    """One notification-policy evaluation and delivery pass.

    No provider calls -- but, since D4 shipped `HttpTelegramClient` on
    2026-08-25, delivery here does reach the real Telegram API whenever the
    global `NEMSEI_V2_NOTIFICATIONS` capability is on, a bot token is mounted
    and the channel is enabled. This docstring said the opposite until
    2026-08-31, which is roughly how long the deployment believed it too.

    The capability is resolved inside `evaluate_and_process_notifications`
    from the process environment rather than passed from here, so every entry
    point -- this job, the digest, and any future caller -- is gated by the
    same switch without having to remember to forward it.
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
