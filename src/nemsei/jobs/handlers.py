from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from nemsei.config import Settings
from nemsei.diagnostics.incidents import evaluate_and_persist_incidents
from nemsei.integrations.fusionsolar.device_status import FusionSolarDeviceStatusService
from nemsei.integrations.fusionsolar.monitoring import FusionSolarMonitoringService
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.integrations.huawei_scada.abandonment import scada_session_liveness
from nemsei.integrations.huawei_scada.retention import purge_samples
from nemsei.integrations.huawei_scada.rollup import HuaweiScadaRollupService
from nemsei.integrations.sigenergy.monitoring import SigenergyMonitoringService
from nemsei.integrations.sigenergy.production import SigenergyProductionService
from nemsei.providers.errors import ProviderErrorCode
from nemsei.reporting.close import close_reporting_months
from nemsei.providers.models import ProviderConnection
from nemsei.providers.registry import ProviderCode
from nemsei.integrations.fusionsolar.session_cache import default_session_cache
from nemsei.jobs.repository import ClaimedJob
from nemsei.notifications.digests import deliver_digest, generate_digest
from nemsei.notifications.episodes import sync_episodes
from nemsei.notifications.service import evaluate_and_process_notifications
from nemsei.sync.abandonment import sweep_abandoned_sync_runs
from nemsei.sync.models import SyncRun
from nemsei.sync.service import active_cooldown_until
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
    def __init__(self, message: str, *, resume_payload: dict[str, Any] | None = None, event_metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        # Durable progress to write into the job's payload before it retries,
        # independent of whether the failure was a cooldown. A batch that
        # already landed stays landed no matter which error path re-queues
        # the job around it.
        self.resume_payload = resume_payload
        self.event_metadata = event_metadata


class DeferredJobError(RuntimeError):
    """The provider is holding this connection back until a known moment.

    Distinct from `RetryableJobError` because the two mean opposite things
    about what just happened. A retryable error is work that was attempted and
    went wrong. This is work that was **not attempted**: the persisted cooldown
    refused it before any HTTP call, so there is nothing to count against the
    job and nothing to learn from trying again sooner.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_at: datetime,
        called_provider: bool,
        resume_payload: dict[str, Any] | None = None,
        event_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_at = retry_at
        # Whether this attempt actually reached the provider. A refusal that
        # cost a real call has been paid for and keeps its attempt; one that
        # never left the process has not, and must not.
        self.called_provider = called_provider
        self.resume_payload = resume_payload
        self.event_metadata = event_metadata


def _cooldown_defer(
    session_factory: sessionmaker[Session],
    *,
    connection_id: int,
    sync_run_id: int,
    error_code: str | None,
    label: str,
    resume_payload: dict[str, Any] | None = None,
    event_metadata: dict[str, Any] | None = None,
) -> None:
    """Raise `DeferredJobError` when a rate limit has a real future cooldown.

    Only a rate limit, and only when a cooldown is actually persisted and still
    ahead: anything else stays on the ordinary retry path, because deferring it
    would mean waiting on something nothing scheduled to change.

    The run's own call count decides whether this attempt was paid for. The
    first refusal of a cycle costs one real request and keeps its attempt; the
    retries that follow inside the cooldown are turned away by
    `reserve_request` before any HTTP, and those are the ones that were being
    charged for a call nobody made.
    """
    if error_code != ProviderErrorCode.RATE_LIMITED.value:
        return
    with session_factory() as session:
        retry_at = active_cooldown_until(session, provider_connection_id=connection_id)
        run = session.get(SyncRun, sync_run_id)
        calls = int((run.metadata_json or {}).get("actual_provider_calls") or 0) if run is not None else 0
    if retry_at is None:
        return
    raise DeferredJobError(
        f"{label} deferred until the provider cooldown expires.",
        retry_at=retry_at,
        called_provider=calls > 0,
        resume_payload=resume_payload,
        event_metadata=event_metadata,
    )


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
    if job.job_type == "sync_runs.sweep_abandoned":
        if settings is None or session_factory is None:
            raise ValueError("The sync run sweep requires worker settings and sessions.")
        return _execute_sync_run_sweep(settings=settings, session_factory=session_factory)
    if job.job_type == "diagnostics.evaluate_incidents":
        if session_factory is None:
            raise ValueError("Diagnostic incident evaluation requires a worker session factory.")
        return _execute_incident_evaluation(session_factory=session_factory)
    if job.job_type == "notifications.process":
        if session_factory is None:
            raise ValueError("Notification processing requires a worker session factory.")
        return _execute_notification_processing(session_factory=session_factory)
    if job.job_type == "reporting.month_close":
        if session_factory is None:
            raise ValueError("Report month close requires a worker session factory.")
        return _execute_report_month_close(session_factory=session_factory)
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


def _execute_report_month_close(*, session_factory: sessionmaker[Session]) -> JobOutcome:
    """Finalise every provisional month whose source data has since closed.

    Reads persisted facts and writes report snapshots. No provider client is
    reachable from here, which is what makes it safe to run on a schedule
    against an account that rate-limits.
    """
    with session_factory() as session, session.begin():
        outcome = close_reporting_months(session)
    return JobOutcome(status="success", result=outcome.as_result())


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
            batch_checkpoint=job.payload.get("backfill_batch_checkpoint"),
        )
    result_json = {"mode": result.mode, "result_status": result.status}
    if result.status in {"failed", "rate_limited", "deferred", "partial"}:
        # A batch checkpoint is real, committed progress -- facts a provider
        # call already landed -- and it belongs in the job's payload before
        # this attempt re-queues, regardless of which error path does the
        # re-queuing. A cooldown defer and an ordinary retry both carry it.
        resume_payload = None
        event_metadata = None
        if result.batch_checkpoint is not None:
            resume_payload = dict(job.payload)
            resume_payload["backfill_batch_checkpoint"] = result.batch_checkpoint
            checkpoint = result.batch_checkpoint
            event_metadata = {
                "source_day": checkpoint.get("source_day"),
                "mapping_count": checkpoint.get("mapping_count"),
                "batch_size": checkpoint.get("batch_size"),
                "batch_count": checkpoint.get("batch_count"),
                "next_batch": checkpoint.get("next_batch"),
                "batch_persisted": (checkpoint.get("batches_done") or 0) > 0,
            }
        _cooldown_defer(
            session_factory,
            connection_id=connection_id,
            sync_run_id=result.sync_run_id,
            error_code=result.error_code,
            label=f"Production {result.mode}",
            resume_payload=resume_payload,
            event_metadata=event_metadata,
        )
        raise RetryableJobError(
            f"Production {result.mode} stopped with {result.status}.",
            resume_payload=resume_payload,
            event_metadata=event_metadata,
        )
    if result.next_source_day:
        payload = dict(job.payload)
        payload["next_source_day"] = result.next_source_day.isoformat()
        payload["mode"] = result.mode
        # Moving on to a new day; any batch checkpoint belonged to the one
        # just finished and must not be read back for a different day.
        payload.pop("backfill_batch_checkpoint", None)
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
        _cooldown_defer(
            session_factory,
            connection_id=connection_id,
            sync_run_id=result.sync_run_id,
            error_code=result.error_code,
            label="Device status poll",
        )
        raise RetryableJobError(f"Device status poll stopped with {result.status}.")
    return JobOutcome(status="success", result=result_json)


def _execute_sync_run_sweep(*, settings: Settings, session_factory: sessionmaker[Session]) -> JobOutcome:
    """Close sync runs whose owner can no longer finish them. Zero provider calls.

    This is where the provider-neutral sweep meets the one adapter that has
    its own liveness evidence: `nemsei.sync.abandonment` knows about provider
    request attempts and nothing else, and the Huawei SCADA resolver is handed
    to it here because a dongle session makes no outbound call to leave an
    attempt behind. Composing them in the handler is what keeps `nemsei.sync`
    from importing an adapter.
    """
    sweep = sweep_abandoned_sync_runs(
        session_factory,
        silence_grace=timedelta(minutes=settings.sync_run_sweep_silence_grace_minutes),
        owner_resolvers=(scada_session_liveness,),
    )
    # Two integers, because `safe_metadata` keeps a strict allowlist and a
    # nested list would be dropped without saying so. What was abandoned, and
    # why, is written on each `sync_run` itself -- which is where an operator
    # looking at one of them will actually be.
    return JobOutcome(
        status="success",
        result={"runs_examined": sweep.examined, "runs_abandoned": sweep.abandoned_count},
    )


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
    """One diagnostic-incident evaluation pass, then one `NotificationEpisode`
    reconciliation pass (Telegram O&M redesign) -- same transaction, same
    restart-safety argument: a failure here never writes a partial row,
    because both `evaluate_and_persist_incidents` and `sync_episodes` flush
    inside the caller's transaction and the whole job either commits or rolls
    back as one unit. `sync_episodes` runs unconditionally, in the worker
    rather than behind its own settings flag: it makes no provider call and
    no Telegram call, it only reconciles `notification_episodes` against
    `diagnostic_incidents`, which is exactly the kind of derived bookkeeping
    the incident evaluator itself already is.
    """
    with session_factory() as session, session.begin():
        summary = evaluate_and_persist_incidents(session)
        episode_summary = sync_episodes(session)
    return JobOutcome(
        status="success",
        result={
            "assets_evaluated": summary.assets_evaluated,
            "incidents_opened": summary.incidents_opened,
            "incidents_confirmed": summary.incidents_confirmed,
            "incidents_resolved": summary.incidents_resolved,
            "deferred": summary.deferred,
            "episodes_created": episode_summary.episodes_created,
            "episodes_confirmed": episode_summary.episodes_confirmed,
            "episodes_reopened": episode_summary.episodes_reopened,
            "episodes_closed": episode_summary.episodes_closed,
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
