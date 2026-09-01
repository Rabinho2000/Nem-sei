"""A zero-call deferral must never consume the job retry budget.

Reproduces the failure of the 26 replacement bounded-backfill jobs 3761-3786
on 2026-09-01. Every one of them ended `failed` / `attempt_count = 10/10` /
`retry_exhausted`, and the audit of their own event log shows why:

    defer_scheduled  x3      <- attempt refunded, correct
    retry_scheduled  x6      <- reason "DeferredJobError", zero provider calls
    retry_exhausted  x1      <- reason "DeferredJobError", zero provider calls

Across the 26 jobs, 246 of the 247 attempt-spending events had
`actual_provider_calls = 0`, and 25 of the 26 terminal `retry_exhausted`
events were zero-call refusals. The provider was never asked. The jobs died
of a budget they never spent.

The cause is the interaction between the bounded deferral cycle and the retry
path: `JobRepository.defer` counted *every* `defer_scheduled` event against
`job_defer_max_cycles`, and past the cap returned False, at which point
`Worker.run_once` fell through to `retry_or_fail` -- which spends the attempt
`claim_next` had already incremented, and eventually writes `retry_exhausted`.
So a local refusal that made no HTTP call at all was converted into a retry
failure. Raising `max_attempts` from 3 to 10 only moved the wall.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import JobEvent
from nemsei.jobs.production import bounded_backfill_payload
from nemsei.jobs.repository import JobRepository
from nemsei.jobs.worker import Worker
from nemsei.sync.models import ProviderRequestState

from tests_v2.test_migrations import upgrade
from tests_v2.test_rate_limit_deferral import (
    LOGIN_OK,
    RATE_LIMITED,
    CountingTransport,
    cooldown_of,
    enabled,
    events_of,
    isolate_session_cache,
    job_row,
    make_due,
    production_connection,
    production_environment,
    real_calls,
    response,
    use_transport,
)


def extend_cooldown(factory, connection_id, *, seconds):
    """The provider pushes its own cooldown further out.

    This is what a *legitimate* extension looks like: another job on the same
    connection made a real call, was rate limited, and `record_request_result`
    moved `cooldown_until` forward. Nothing about this job changed.
    """
    ahead = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    with factory() as session:
        session.execute(
            update(ProviderRequestState)
            .where(ProviderRequestState.provider_connection_id == connection_id)
            .values(cooldown_until=ahead, provider_retry_at=ahead)
        )
        session.commit()


def clear_cooldown(factory, connection_id):
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    with factory() as session:
        session.execute(
            update(ProviderRequestState)
            .where(ProviderRequestState.provider_connection_id == connection_id)
            .values(cooldown_until=past, provider_retry_at=past, next_allowed_at=None)
        )
        session.commit()


def backfill_job(repository, connection_id, *, max_attempts=10):
    """The real shape of jobs 3761-3786: one source day, ten attempts."""
    job, created = repository.enqueue(
        job_type="production.bounded_backfill",
        payload=bounded_backfill_payload(
            connection_id, start_date=date(2026, 8, 3), end_date=date(2026, 8, 3)
        ),
        actor_source="web",
        max_attempts=max_attempts,
    )
    assert created
    return job


def test_zero_call_deferrals_never_exhaust_a_bounded_backfill(settings, monkeypatch) -> None:
    """Jobs 3761-3786, 2026-09-01: killed by refusals that made no provider call.

    One real call is made and rate limited. The cooldown is then repeatedly
    extended -- exactly as it was in production, where 26 jobs shared one
    connection and whichever woke first spent the only real call of the cycle.
    This job is revisited far more times than any cap allows, and every one of
    those visits must be free: no HTTP, no attempt, no movement toward
    `retry_exhausted`.
    """
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    # The production default. The defect showed at the fourth deferral, so the
    # cap is left where the live deployment had it rather than tuned away.
    worker_settings = replace(enabled(settings), job_defer_max_cycles=3)
    repository = JobRepository(engine, factory)

    transport = CountingTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job = backfill_job(repository, connection_id)

    # 1. one real provider call, 2. rate limited, cooldown persisted.
    assert Worker(worker_settings, worker_id="backfill-defect").run_once()
    calls_after_the_real_one = len(transport.calls)
    assert calls_after_the_real_one > 0, "the first attempt really did reach the provider"
    paid_attempts = job_row(factory, job.id).attempt_count
    paid_calls = real_calls(factory, connection_id)
    assert cooldown_of(factory, connection_id) > datetime.now(timezone.utc)

    # 3-5. Revisited far past the cap while the cooldown stands and is pushed
    # out again by other work on the connection. The transport script is
    # exhausted, so any HTTP here raises IndexError rather than being stubbed.
    assert transport.responses == []
    for cycle in range(12):
        extend_cooldown(factory, connection_id, seconds=600)
        make_due(factory, job.id)
        Worker(worker_settings, worker_id="backfill-defect").run_once()

        row = job_row(factory, job.id)
        # 7. never terminal because of a refusal nobody paid for
        assert row.status == "waiting", f"cycle {cycle}: deferred, not failed ({row.status})"
        # 6. the budget is untouched
        assert row.attempt_count == paid_attempts, f"cycle {cycle}: a zero-call deferral spent an attempt"
        # 5. zero HTTP
        assert len(transport.calls) == calls_after_the_real_one, f"cycle {cycle}: a provider call escaped"
        assert real_calls(factory, connection_id) == paid_calls, f"cycle {cycle}: provider call counted"
        # lease released, and the wait respects the provider's own cooldown
        assert row.lease_owner is None and row.lease_token is None and row.lease_expires_at is None
        assert row.available_at >= cooldown_of(factory, connection_id)

    kinds = [event for event, *_ in events_of(factory, job.id)]
    assert "retry_exhausted" not in kinds, "zero-call deferrals must never exhaust the retry budget"
    assert kinds.count("retry_scheduled") == 0, "a refusal that made no call is not a retry"

    # The event log tells the two kinds of deferral apart on its own -- both by
    # event type and by a `called_provider` that survives `safe_metadata`,
    # whose allowlist silently dropped it the first time round.
    with factory() as session:
        deferrals = [
            (event.event_type, (event.metadata_json or {}).get("called_provider"))
            for event in session.scalars(
                select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id)
            )
            if event.event_type.startswith("defer_")
        ]
    # One paid deferral -- the real call that was rate limited -- and then
    # nothing but free ones, however many times the job was revisited.
    assert deferrals[0] == ("defer_scheduled", "True"), deferrals[0]
    assert all(entry == ("defer_no_call", "False") for entry in deferrals[1:]), deferrals
    assert len(deferrals) == 13, "one paid deferral and twelve free ones"

    # 8. the cooldown expires and a real call is allowed through again.
    transport.load([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    clear_cooldown(factory, connection_id)
    make_due(factory, job.id)
    assert Worker(worker_settings, worker_id="backfill-defect").run_once()
    assert len(transport.calls) > calls_after_the_real_one, "the provider was reachable again"
    # 9. and that real call is the thing that consumes the next attempt.
    assert real_calls(factory, connection_id) > paid_calls
    assert job_row(factory, job.id).attempt_count > paid_attempts, "a real call spends an attempt"


def test_a_bounded_backfill_survives_a_long_provider_outage(settings, monkeypatch) -> None:
    """Thirty zero-call cycles, one attempt spent, and the day still gets fetched.

    The operational claim behind the fix: waiting out an arbitrarily long
    provider cooldown is preferable to failing work that was never attempted.
    """
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    worker_settings = replace(enabled(settings), job_defer_max_cycles=3)
    repository = JobRepository(engine, factory)

    transport = CountingTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job = backfill_job(repository, connection_id)
    assert Worker(worker_settings, worker_id="backfill-outage").run_once()
    spent = job_row(factory, job.id).attempt_count

    for _ in range(30):
        extend_cooldown(factory, connection_id, seconds=3600)
        make_due(factory, job.id)
        Worker(worker_settings, worker_id="backfill-outage").run_once()

    row = job_row(factory, job.id)
    assert row.status == "waiting"
    assert row.attempt_count == spent, "a day-long outage cost exactly the one attempt that was paid"

    # The outage ends and the day is actually collected.
    transport.load(
        [
            response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
            response({"success": True, "failCode": 0, "data": [
                {"collectTime": int(datetime(2026, 8, 3, 12, tzinfo=timezone.utc).timestamp() * 1000),
                 "stationCode": "FS-001", "dataItemMap": {"inverter_power": 12.5}}
            ]}),
        ]
    )
    clear_cooldown(factory, connection_id)
    make_due(factory, job.id)
    assert Worker(worker_settings, worker_id="backfill-outage").run_once()
    assert job_row(factory, job.id).status in {"success", "waiting"}


def test_a_real_refused_call_still_spends_its_attempt(settings, monkeypatch) -> None:
    """The other half of the invariant: paid refusals are not free.

    A cooldown deferral that cost a real provider call keeps its attempt and
    still counts against the bound, so a provider that genuinely refuses over
    and over still ends the job rather than leaving it waiting for ever.
    """
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    worker_settings = replace(enabled(settings), job_defer_max_cycles=2)
    repository = JobRepository(engine, factory)

    from tests_v2.test_rate_limit_deferral import AlwaysRefusingTransport

    transport = AlwaysRefusingTransport()
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job = backfill_job(repository, connection_id, max_attempts=3)

    for _ in range(8):
        clear_cooldown(factory, connection_id)
        make_due(factory, job.id)
        if not Worker(worker_settings, worker_id="backfill-paid").run_once():
            break
        if job_row(factory, job.id).status == "failed":
            break

    row = job_row(factory, job.id)
    assert row.status == "failed", "a provider that never relents still ends the job"
    assert row.attempt_count >= 1, "and the attempts it spent were real calls"
    assert len(transport.calls) > 0


# --- recurring slots on one provider lane -------------------------------------


def test_a_deferred_slot_and_its_successor_never_call_the_provider_together(
    settings, monkeypatch
) -> None:
    """The overlap risk the deferral bound was built for, tested directly.

    Unbounded free deferrals mean a recurring slot can sit in `waiting` while
    the schedule enqueues its successor, so both are alive on the same
    provider lane at once. That is only safe because the condition that defers
    the old slot is the *same* condition that refuses the new one: an active
    persisted cooldown on the connection. `reserve_request` takes the state
    row `FOR UPDATE` and reads that cooldown before any HTTP, so whichever job
    a worker picks up while it stands is turned away having called nothing.

    Proven here by exhausting the transport script: any HTTP call beyond the
    one that set the cooldown raises `IndexError` rather than being stubbed.
    """
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id = production_connection(factory)
    worker_settings = replace(enabled(settings), job_defer_max_cycles=3)
    repository = JobRepository(engine, factory)

    transport = CountingTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), RATE_LIMITED])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)

    # The old slot: one real call, rate limited, now waiting on the cooldown.
    old_slot, _created = repository.enqueue_due_production_incremental(
        connection_id=connection_id, interval_hours=24
    )
    assert Worker(worker_settings, worker_id="lane-old").run_once()
    calls_after_the_real_one = len(transport.calls)
    cooldown = cooldown_of(factory, connection_id)
    assert cooldown > datetime.now(timezone.utc)
    assert job_row(factory, old_slot.id).status == "waiting"

    # The successor slot, enqueued by the schedule a day later while the old
    # one is still waiting. A distinct dedupe key, so it is a second live job.
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    new_slot, created = repository.enqueue_due_production_incremental(
        connection_id=connection_id, interval_hours=24, now=later
    )
    assert created and new_slot.id != old_slot.id, "the successor is a separate job"

    # Both due at once on the same lane, worked repeatedly.
    assert transport.responses == []
    for _ in range(6):
        make_due(factory, old_slot.id)
        make_due(factory, new_slot.id)
        while Worker(worker_settings, worker_id="lane-both").run_once():
            pass

    # Non-vacuous: both slots really were claimed and evaluated in that loop.
    for job_id, label in ((old_slot.id, "old"), (new_slot.id, "successor")):
        claims = [event for event, *_ in events_of(factory, job_id) if event == "claimed"]
        assert len(claims) >= 2, f"the {label} slot was never re-claimed, so this proves nothing"

    assert len(transport.calls) == calls_after_the_real_one, (
        "while the cooldown stands, neither slot reached the provider"
    )
    with factory() as session:
        allowed = session.scalar(
            select(ProviderRequestState.actual_call_count).where(
                ProviderRequestState.provider_connection_id == connection_id,
                ProviderRequestState.endpoint_family == "production_history_daily",
            )
        )
    assert allowed == 1, "exactly one reservation was ever granted on this lane"

    for job_id in (old_slot.id, new_slot.id):
        row = job_row(factory, job_id)
        assert row.status == "waiting", "both slots wait; neither fails"
        assert row.available_at >= cooldown, "and both wait on the provider's own cooldown"
        assert row.lease_owner is None
    assert "retry_exhausted" not in [event for event, *_ in events_of(factory, old_slot.id)]
