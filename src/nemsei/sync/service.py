"""Short-transaction services for provider health, syncs, and request deferral."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sync.models import (
    HEALTH_STATES,
    SYNC_RUN_STATUSES,
    IntegrationHealth,
    ProviderRequestAttempt,
    ProviderRequestState,
    SyncCursor,
    SyncRun,
)
from nemsei.sync.repository import SyncRepository


# How long a rate-limited endpoint family is held back when the provider does
# not say. FusionSolar does not say: it reports its own brake as `failCode 407`
# in a JSON body rather than an HTTP 429 with `Retry-After`, so
# `ProviderError.retry_after_seconds` arrives as None for every real rate limit
# this deployment has ever seen.
#
# That used to be read as `or 0` -- a cooldown that expired the moment it was
# written, which is to say no cooldown at all. On 2026-08-30 the production
# sync was refused at 15:59:07, called the provider again at 16:00:08, and
# again at 16:05:10; the guard was in the code path each time and deferred
# nothing. Every FusionSolar production sync since 2026-08-24 ended the same
# way.
#
# Ten minutes is measured, not chosen: the shared account was observed
# recovering in about ten minutes after a burst of consecutive calls
# (docker-compose.v2.yml's device-status note). Believing a provider that does
# name a window is still strictly better, so an explicit value always wins --
# this is the floor for silence, not a ceiling on the provider.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 600


def _detail(value: str | None) -> str | None:
    return value[:500] if value else None


def health_values_for_error(error: ProviderError | None, *, operation: str) -> dict[str, str]:
    """Map provider outcomes to neutral integration-health dimensions."""
    operation_field = f"{operation}_state"
    if error is None:
        values = {"auth_state": "healthy", "access_state": "healthy", "provider_state": "healthy", "quota_state": "unknown"}
        values[operation_field] = "healthy"
        return values
    if error.code is ProviderErrorCode.CONFIGURATION:
        values = {"auth_state": "not_configured", "access_state": "not_configured", "provider_state": "not_configured", "quota_state": "unknown"}
        values[operation_field] = "not_configured"
        return values
    values = {"auth_state": "unknown", "access_state": "unknown", "provider_state": "unknown", "quota_state": "unknown"}
    if error.code is ProviderErrorCode.AUTHENTICATION:
        values["auth_state"] = "degraded"
    elif error.code is ProviderErrorCode.AUTHORIZATION:
        values["access_state"] = "degraded"
    elif error.code is ProviderErrorCode.RATE_LIMITED:
        values["provider_state"] = "healthy"
        values["quota_state"] = "degraded"
    elif error.code in {ProviderErrorCode.UNAVAILABLE, ProviderErrorCode.TIMEOUT, ProviderErrorCode.TRANSPORT}:
        values["provider_state"] = "unavailable"
    values[operation_field] = "degraded"
    return values


def health_for(session: Session, provider_connection_id: int) -> IntegrationHealth:
    health = SyncRepository(session).health(provider_connection_id)
    if health is None:
        health = IntegrationHealth(provider_connection_id=provider_connection_id, updated_at=utc_now())
        session.add(health)
        session.flush()
    return health


def record_health(
    session: Session,
    *,
    provider_connection_id: int,
    auth_state: str | None = None,
    access_state: str | None = None,
    provider_state: str | None = None,
    discovery_state: str | None = None,
    quota_state: str | None = None,
    sync_state: str | None = None,
    partial: bool | None = None,
    stale: bool | None = None,
    error: ProviderError | None = None,
) -> IntegrationHealth:
    health = health_for(session, provider_connection_id)
    values = {
        "auth_state": auth_state,
        "access_state": access_state,
        "provider_state": provider_state,
        "discovery_state": discovery_state,
        "quota_state": quota_state,
        "sync_state": sync_state,
    }
    if any(value is not None and value not in HEALTH_STATES for value in values.values()):
        raise ValueError("Invalid integration health state")
    for field, value in values.items():
        if value is not None:
            setattr(health, field, value)
    if partial is not None:
        health.partial = partial
    if stale is not None:
        health.stale = stale
    now = utc_now()
    health.last_attempt_at = now
    if error is None:
        health.last_success_at = now
        health.last_error_code = None
    else:
        health.last_failure_at = now
        health.last_error_code = error.code.value
    health.updated_at = now
    return health


def start_sync_run(
    session: Session,
    *,
    provider_connection_id: int,
    capability: str,
    requested_from: datetime | None = None,
    requested_until: datetime | None = None,
) -> SyncRun:
    run = SyncRun(
        provider_connection_id=provider_connection_id,
        capability=capability,
        status="running",
        requested_from=as_utc(requested_from) if requested_from else None,
        requested_until=as_utc(requested_until) if requested_until else None,
        started_at=utc_now(),
        completeness="unknown",
        metadata_json={},
    )
    session.add(run)
    record_health(session, provider_connection_id=provider_connection_id, sync_state="healthy")
    return run


def finish_sync_run(
    session: Session,
    *,
    run: SyncRun,
    status: str,
    completeness: str = "unknown",
    error: ProviderError | None = None,
    safe_detail: str | None = None,
) -> SyncRun:
    if status not in SYNC_RUN_STATUSES or status in {"pending", "running"}:
        raise ValueError("Invalid terminal sync run status")
    if run.status != "running":
        raise ValueError("Only a running sync run can finish")
    run.status = status
    run.completeness = completeness
    run.finished_at = utc_now()
    run.error_code = error.code.value if error else None
    run.safe_detail = _detail(safe_detail or (error.safe_message if error else None))
    health = health_for(session, run.provider_connection_id)
    health.last_attempt_at = run.finished_at
    health.sync_state = "healthy" if status == "success" else "degraded"
    health.partial = status == "partial"
    if status == "success":
        health.last_success_at = run.finished_at
        health.last_successful_sync_at = run.finished_at
        health.last_error_code = None
    elif status in {"failed", "rate_limited", "deferred"}:
        health.last_failure_at = run.finished_at
        health.last_error_code = error.code.value if error else None
    health.updated_at = run.finished_at
    return run


def advance_cursor(
    session: Session,
    *,
    run: SyncRun,
    cursor_key: str,
    checkpoint: dict,
    covered_through: datetime | None,
) -> SyncCursor:
    if run.status != "success":
        raise ValueError("Only a successful sync can advance coverage")
    cursor = SyncRepository(session).cursor(provider_connection_id=run.provider_connection_id, capability=run.capability, cursor_key=cursor_key)
    now = utc_now()
    if cursor is None:
        cursor = SyncCursor(
            provider_connection_id=run.provider_connection_id,
            capability=run.capability,
            cursor_key=cursor_key,
            checkpoint_json=dict(checkpoint),
            covered_through=as_utc(covered_through) if covered_through else None,
            last_successful_run_id=run.id,
            updated_at=now,
        )
        session.add(cursor)
        return cursor
    proposed = as_utc(covered_through) if covered_through else None
    if cursor.covered_through and proposed and proposed < as_utc(cursor.covered_through):
        raise ValueError("Sync cursor coverage cannot move backwards")
    cursor.checkpoint_json = dict(checkpoint)
    cursor.covered_through = proposed
    cursor.last_successful_run_id = run.id
    cursor.updated_at = now
    return cursor


def reserve_request(
    session: Session,
    *,
    provider_connection_id: int,
    endpoint_family: str,
    purpose: str,
    sync_run_id: int | None = None,
    now: datetime | None = None,
) -> tuple[ProviderRequestState, ProviderRequestAttempt, bool]:
    now_value = as_utc(now or utc_now())
    # PostgreSQL is the sole V2 runtime. Create-or-lock prevents concurrent
    # workers from both reserving an initial request-state row or losing its
    # actual-call counter/cooldown update.
    session.execute(
        insert(ProviderRequestState)
        .values(
            provider_connection_id=provider_connection_id,
            endpoint_family=endpoint_family,
            quota_known=False,
            actual_call_count=0,
            updated_at=now_value,
        )
        .on_conflict_do_nothing(index_elements=("provider_connection_id", "endpoint_family"))
    )
    state = session.scalar(
        select(ProviderRequestState)
        .where(
            ProviderRequestState.provider_connection_id == provider_connection_id,
            ProviderRequestState.endpoint_family == endpoint_family,
        )
        .with_for_update()
    )
    assert state is not None
    deferred_until = max((value for value in (state.next_allowed_at, state.cooldown_until, state.provider_retry_at) if value is not None), default=None)
    allowed = deferred_until is None or as_utc(deferred_until) <= now_value
    attempt = ProviderRequestAttempt(
        request_state_id=state.id,
        sync_run_id=sync_run_id,
        purpose=purpose[:120],
        status="reserved" if allowed else "deferred",
        occurred_at=now_value,
        retry_after_at=deferred_until,
        safe_detail=None if allowed else "request deferred by persisted provider state",
    )
    if allowed:
        state.actual_call_count += 1
        state.last_attempt_at = now_value
    state.updated_at = now_value
    session.add(attempt)
    return state, attempt, allowed


def record_request_result(
    session: Session,
    *,
    state: ProviderRequestState,
    attempt: ProviderRequestAttempt,
    error: ProviderError | None = None,
    now: datetime | None = None,
    default_rate_limit_cooldown_seconds: int = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
) -> None:
    now_value = as_utc(now or utc_now())
    if attempt.status not in {"reserved", "deferred"}:
        raise ValueError("Request attempt already finalized")
    if error is None:
        attempt.status = "succeeded"
        state.last_success_at = now_value
    elif error.code is ProviderErrorCode.RATE_LIMITED:
        attempt.status = "rate_limited"
        # `is None` rather than `or`: a provider that explicitly says zero is
        # believed, and only silence falls back to the default.
        cooldown_seconds = (
            error.retry_after_seconds
            if error.retry_after_seconds is not None
            else default_rate_limit_cooldown_seconds
        )
        retry_at = now_value + timedelta(seconds=cooldown_seconds)
        attempt.retry_after_at = retry_at
        state.provider_retry_at = retry_at
        state.cooldown_until = retry_at
    else:
        attempt.status = "failed"
        attempt.safe_detail = _detail(error.safe_message)
    state.updated_at = now_value
