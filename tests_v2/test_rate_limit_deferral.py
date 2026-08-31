"""A provider cooldown is a defer, not a failure.

Both of these reproduce a job that ended `failed` in production on 2026-08-31
for a reason that had nothing to do with the work being wrong:

* the provider rate limited one real call, and the persisted cooldown was set
  600 seconds ahead (`sync/service.py`);
* the generic job retry fired at +60s and +300s, both still inside that
  cooldown;
* both were correctly refused locally, with **no HTTP call at all** -- the
  guard worked;
* but each refusal was counted as an attempt, so the third one exhausted
  `max_attempts` and the job failed at 16:25:12 (production) and 16:38:03
  (device status), four and four minutes before the cooldown expired.

The provider never said no to those two attempts. Nothing was even asked. So
they must not spend the retry budget, and the job must wait for the cooldown
rather than burning through it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, update

from nemsei.assets.service import create_asset, create_device
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, HttpResponse
from nemsei.jobs import handlers
from nemsei.jobs.models import Job, JobEvent
from nemsei.jobs.repository import JobRepository
from nemsei.jobs.worker import Worker
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import ProviderRequestAttempt, ProviderRequestState, SyncCursor
from tests_v2.test_migrations import upgrade


LOGIN_OK = {"success": True, "failCode": 0, "data": None}

# What FusionSolar actually returns for its own frequency brake: a JSON body,
# no `Retry-After` header, so `retry_after_seconds` is None and the cooldown
# falls back to the measured 600 seconds.
RATE_LIMITED = FusionSolarClientError(
    ProviderError(ProviderErrorCode.RATE_LIMITED, "FusionSolar rate limited the request.", transient=True)
)


class CountingTransport:
    """Records every HTTP call. An exhausted script is a hard failure, not a stub.

    That is the point in these tests: after the deferral, any attempt to reach
    the provider raises `IndexError` instead of quietly returning something, so
    "zero HTTP during the cooldown" is proved rather than asserted.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, payload, headers, timeout_seconds):
        self.calls.append(url.rsplit("/", 1)[-1])
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def load(self, responses):
        """Re-arm for the call that is allowed to happen after the cooldown."""
        self.responses = list(responses)


class AlwaysRefusingTransport:
    """Logs in when asked, refuses every read, for as many cycles as it takes.

    A scripted list cannot express that: once a session is cached the login is
    skipped, so a fixed script drifts out of alignment on the second cycle.
    This answers by endpoint instead, which is what "the provider is refusing
    indefinitely" actually means.
    """

    def __init__(self):
        self.calls = []

    def post(self, url, payload, headers, timeout_seconds):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint == "login":
            return HttpResponse(200, {"XSRF-TOKEN": "t"}, LOGIN_OK)
        raise RATE_LIMITED


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, payload)


def enabled(settings):
    return replace(settings, capabilities={**settings.capabilities, "provider_reads": True})


def isolate_session_cache(monkeypatch):
    """Give this test its own FusionSolar session cache.

    `default_session_cache()` is a process-wide singleton by design -- the
    worker is meant to reuse one login across jobs. In a test run that makes
    tests order-dependent: these fixtures share a base URL and username, so a
    login cached by an earlier test let a later one skip its own and consume
    the wrong scripted response.
    """
    from nemsei.integrations.fusionsolar import session_cache as module

    monkeypatch.setattr(module, "_default_cache", module.FusionSolarSessionCache())


def use_transport(monkeypatch, attribute, transport):
    """Point one handler's service at a fake transport, leaving the rest real.

    The handler builds its own service, so this is the only seam. Everything
    below it -- the request controller, the persisted cooldown, the job
    repository and the worker loop -- is the production code path.
    """
    real = getattr(handlers, attribute)

    def build(session_factory, service_settings, **kwargs):
        kwargs.pop("client_factory", None)
        return real(
            session_factory,
            service_settings,
            client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport),
            **kwargs,
        )

    monkeypatch.setattr(handlers, attribute, build)


def cooldown_of(factory, connection_id):
    """The latest cooldown across the connection's families. NULLs excluded:
    PostgreSQL sorts them first on DESC, which would hide a real one."""
    with factory() as session:
        return session.scalar(
            select(ProviderRequestState.cooldown_until)
            .where(
                ProviderRequestState.provider_connection_id == connection_id,
                ProviderRequestState.cooldown_until.is_not(None),
            )
            .order_by(ProviderRequestState.cooldown_until.desc())
            .limit(1)
        )


def job_row(factory, job_id):
    with factory() as session:
        job = session.get(Job, job_id)
        session.expunge(job)
        return job


def real_calls(factory, connection_id):
    """Only attempts that actually reached the provider increment this."""
    with factory() as session:
        return session.scalar(
            select(func.sum(ProviderRequestState.actual_call_count)).where(
                ProviderRequestState.provider_connection_id == connection_id
            )
        )


def expire_cooldown(factory, connection_id):
    """Time passes. Nothing else about the job or the provider state changes."""
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    with factory() as session:
        session.execute(
            update(ProviderRequestState)
            .where(ProviderRequestState.provider_connection_id == connection_id)
            .values(cooldown_until=past, provider_retry_at=past, next_allowed_at=None)
        )
        session.commit()


def make_due(factory, job_id):
    """The job's own wait is over -- `available_at` has arrived."""
    with factory() as session:
        session.execute(
            update(Job).where(Job.id == job_id).values(available_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        session.commit()


def events_of(factory, job_id):
    with factory() as session:
        return [
            (event.event_type, event.attempt, event.from_status, event.to_status)
            for event in session.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id))
        ]


# --- production.incremental ---------------------------------------------------


def production_connection(factory):
    with factory() as session:
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="fusion-production",
            display_name="Fusion production",
            credential_reference="production",
            enabled=True,
            configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Deferral production asset")
        mapping = create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id="FS-001",
            valid_from=date(2020, 1, 1),
        )
        create_source_policy(
            session,
            asset_id=asset.id,
            provider_mapping_id=mapping.id,
            source_use="production",
            priority=1,
            valid_from=date(2020, 1, 1),
        )
        from nemsei.providers.registry import ProviderCapability
        from nemsei.shared.clock import utc_now

        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).date()
        session.add(
            SyncCursor(
                provider_connection_id=connection.id,
                capability=ProviderCapability.PRODUCTION_HISTORY.value,
                cursor_key="fusionsolar-daily-production",
                checkpoint_json={"last_completed_day": yesterday.isoformat(), "source_timezone": "UTC"},
                covered_through=datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc),
                updated_at=utc_now(),
            )
        )
        session.commit()
        return connection.id


def production_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_BASE_URL", "https://fusion.example.test")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_TIMEZONE", "UTC")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_UNIT", "kWh")
    monkeypatch.setenv("NEMSEI_V2_SKIP_V1_OWNERSHIP_CHECK", "true")


def test_production_incremental_waits_out_a_cooldown_instead_of_failing(settings, monkeypatch) -> None:
    """job 3287, 2026-08-31: real call -> rate limited -> failed before the cooldown ended."""
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    worker_settings = enabled(settings)
    repository = JobRepository(engine, factory)

    transport = CountingTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)

    job, created = repository.enqueue_due_production_incremental(connection_id=connection_id, interval_hours=24)
    assert created and job is not None

    # 1. A real call, and the provider refuses it.
    assert Worker(worker_settings, worker_id="deferral-prod").run_once()
    calls_after_refusal = len(transport.calls)
    assert calls_after_refusal >= 2, "the login and the refused read both happened"

    cooldown = cooldown_of(factory, connection_id)
    assert cooldown > datetime.now(timezone.utc), "the cooldown is real and in the future"

    row = job_row(factory, job.id)
    # 2. waiting until the cooldown, not failed and not retried inside it.
    assert row.status == "waiting"
    assert row.available_at >= cooldown, "the job may not wake before the provider will answer"
    assert row.lease_owner is None and row.lease_token is None and row.lease_expires_at is None
    # 3. The refused call did spend an attempt; what follows must not.
    assert row.attempt_count == 1

    # 4. Zero HTTP during the cooldown. The transport is empty, so any call
    #    raises -- and the worker finds nothing due anyway.
    assert transport.responses == []
    for _ in range(3):
        assert Worker(worker_settings, worker_id="deferral-prod").run_once() is False
    assert len(transport.calls) == calls_after_refusal

    # 5. The cooldown expires. The next claim is a real call again.
    transport.load([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    expire_cooldown(factory, connection_id)
    make_due(factory, job.id)
    assert Worker(worker_settings, worker_id="deferral-prod").run_once()
    assert len(transport.calls) > calls_after_refusal, "a real call happened after the cooldown"

    after = job_row(factory, job.id)
    assert after.status == "waiting", "refused again, deferred again -- still not failed"
    assert after.attempt_count <= 2, "deferrals did not burn the budget"


def test_a_local_deferral_never_spends_an_attempt(settings, monkeypatch) -> None:
    """The precise defect: a refusal that cost no provider call cost an attempt.

    Three deferrals in a row must leave the budget where the one real refusal
    left it, and must write `provider_request_attempts` rows marked `deferred`
    rather than `rate_limited` -- nothing was asked of the provider.
    """
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    # A cap high enough that this test is about the budget and nothing else.
    worker_settings = replace(enabled(settings), job_defer_max_cycles=10)
    repository = JobRepository(engine, factory)

    transport = CountingTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job, _created = repository.enqueue_due_production_incremental(connection_id=connection_id, interval_hours=24)
    assert Worker(worker_settings, worker_id="deferral-budget").run_once()

    baseline_attempts = job_row(factory, job.id).attempt_count
    baseline_calls = real_calls(factory, connection_id)

    # Force the job due repeatedly while the cooldown still stands.
    for _ in range(3):
        make_due(factory, job.id)
        Worker(worker_settings, worker_id="deferral-budget").run_once()

    row = job_row(factory, job.id)
    assert row.status == "waiting", "still deferred, never failed"
    assert row.attempt_count == baseline_attempts, "a deferral is not an attempt"
    assert real_calls(factory, connection_id) == baseline_calls, "no provider call was made"
    assert row.available_at >= cooldown_of(factory, connection_id)

    with factory() as session:
        statuses = list(session.scalars(select(ProviderRequestAttempt.status).order_by(ProviderRequestAttempt.id)))
    assert "deferred" in statuses, "the refusals were local, and recorded as such"

    kinds = {event for event, *_ in events_of(factory, job.id)}
    assert "retry_exhausted" not in kinds


# --- device_status.poll -------------------------------------------------------


def device_connection(factory):
    with factory() as session:
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="fusion-device-status",
            display_name="Fusion device status",
            credential_reference="dev",
            enabled=True,
            configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Deferral device asset")
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="FS-STATION")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", serial_number="SN-1")
        create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id="DEV-001",
            resource_kind="device",
            device_id=device.id,
        )
        session.commit()
        return connection.id


def device_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_BASE_URL", "https://fusion.example.test")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_POWER_UNIT", "kW")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_ENERGY_UNIT", "kWh")
    monkeypatch.setenv("NEMSEI_V2_SKIP_V1_OWNERSHIP_CHECK", "true")


def test_device_status_poll_waits_out_a_cooldown_instead_of_failing(settings, monkeypatch) -> None:
    """job 3300, 2026-08-31: one refused `getDevList`, then two free deferrals, then failed."""
    device_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = device_connection(factory)
    worker_settings = replace(
        enabled(settings),
        device_status_poll_enabled=True,
        device_status_poll_connection_id=connection_id,
        device_status_poll_max_cycles=100,
    )
    repository = JobRepository(engine, factory)

    transport = CountingTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    use_transport(monkeypatch, "FusionSolarDeviceStatusService", transport)

    job, created = repository.enqueue_due_device_status_poll(
        connection_id=connection_id, interval_minutes=30, max_cycles=100
    )
    assert created and job is not None

    assert Worker(worker_settings, worker_id="deferral-device").run_once()
    calls_after_refusal = len(transport.calls)
    cooldown = cooldown_of(factory, connection_id)
    assert cooldown > datetime.now(timezone.utc)

    row = job_row(factory, job.id)
    assert row.status == "waiting"
    assert row.available_at >= cooldown
    assert row.lease_owner is None

    assert transport.responses == []
    for _ in range(3):
        assert Worker(worker_settings, worker_id="deferral-device").run_once() is False
    assert len(transport.calls) == calls_after_refusal, "zero HTTP while the cooldown stands"

    transport.load([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    expire_cooldown(factory, connection_id)
    make_due(factory, job.id)
    assert Worker(worker_settings, worker_id="deferral-device").run_once()
    assert len(transport.calls) > calls_after_refusal
    assert job_row(factory, job.id).status == "waiting"


def test_deferring_is_bounded_so_a_permanent_refusal_cannot_loop_forever(settings, monkeypatch) -> None:
    """Waiting is not the same as never giving up.

    Each deferral costs one real call per cooldown, which is not a storm, but a
    provider that refuses indefinitely must not leave a job waiting for ever
    while the schedule enqueues its successors. Past the cap the job goes back
    to the ordinary retry path and can fail like anything else.
    """
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    worker_settings = replace(enabled(settings), job_defer_max_cycles=2)
    repository = JobRepository(engine, factory)

    transport = AlwaysRefusingTransport()
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job, _created = repository.enqueue_due_production_incremental(connection_id=connection_id, interval_hours=24)

    for _ in range(8):
        expire_cooldown(factory, connection_id)
        make_due(factory, job.id)
        if not Worker(worker_settings, worker_id="deferral-cap").run_once():
            break
        if job_row(factory, job.id).status == "failed":
            break

    row = job_row(factory, job.id)
    assert row.status == "failed", "a provider that never relents eventually fails the job"
    deferrals = [event for event, *_ in events_of(factory, job.id) if event == "defer_scheduled"]
    assert len(deferrals) == 2, "and it deferred exactly as many times as configured"


def test_a_failure_that_is_not_a_cooldown_still_retries_normally(settings, monkeypatch) -> None:
    """Only a persisted future cooldown turns a failure into a wait.

    A transport error has no cooldown behind it, so it must keep the ordinary
    backoff -- deferring it would wait on something that was never scheduled to
    change.
    """
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    worker_settings = enabled(settings)
    repository = JobRepository(engine, factory)

    unavailable = FusionSolarClientError(
        ProviderError(ProviderErrorCode.UNAVAILABLE, "FusionSolar is unavailable.", transient=True)
    )
    transport = CountingTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), unavailable])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job, _created = repository.enqueue_due_production_incremental(connection_id=connection_id, interval_hours=24)
    assert Worker(worker_settings, worker_id="deferral-other").run_once()

    row = job_row(factory, job.id)
    assert row.status == "waiting"
    assert row.attempt_count == 1, "an ordinary retry still spends its attempt"
    kinds = [event for event, *_ in events_of(factory, job.id)]
    assert "retry_scheduled" in kinds and "defer_scheduled" not in kinds
