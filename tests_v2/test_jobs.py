from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import func, select

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job
from nemsei.shared.clock import utc_now
from nemsei.jobs.repository import JobRepository
from tests_v2.test_migrations import upgrade


def repository(settings, monkeypatch) -> JobRepository:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    return JobRepository(engine, build_session_factory(engine))


def test_enqueue_deduplicates_active_jobs_and_audits(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    first, created = repo.enqueue(job_type="system.noop", payload={}, actor_source="web", dedupe_key="same")
    second, duplicate_created = repo.enqueue(job_type="system.noop", payload={}, actor_source="web", dedupe_key="same")
    assert created is True
    assert duplicate_created is False
    assert second.id == first.id
    assert [event.event_type for event in repo.events_for(first.id)] == ["enqueued", "dedupe_reused"]


def test_claim_is_atomic_and_records_worker_actor(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="web")
    first = repo.claim_next(worker_id="worker-a", lease_seconds=30)
    second = repo.claim_next(worker_id="worker-b", lease_seconds=30)
    assert first and first.id == job.id
    assert second is None
    assert repo.events_for(job.id)[-1].actor_source == "worker"


def test_independent_workers_claim_distinct_jobs_with_skip_locked(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    first, _ = repo.enqueue(job_type="system.noop", payload={"number": 1}, actor_source="web")
    second, _ = repo.enqueue(job_type="system.noop", payload={"number": 2}, actor_source="web")

    def claim(worker_id: str):
        engine = build_engine(settings)
        return JobRepository(engine, build_session_factory(engine)).claim_next(worker_id=worker_id, lease_seconds=30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))

    assert {claim.id for claim in claims if claim is not None} == {first.id, second.id}
    assert len({claim.lease_token for claim in claims if claim is not None}) == 2


def test_concurrent_enqueue_returns_one_active_job(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)

    def enqueue_once() -> tuple[int, bool]:
        engine = build_engine(settings)
        concurrent_repo = JobRepository(engine, build_session_factory(engine))
        job, created = concurrent_repo.enqueue(
            job_type="system.noop", payload={}, actor_source="web", dedupe_key="concurrent"
        )
        return job.id, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _unused: enqueue_once(), range(2)))

    assert {job_id for job_id, _created in results} == {results[0][0]}
    assert sum(created for _job_id, created in results) == 1
    assert [event.event_type for event in repo.events_for(results[0][0])] == ["enqueued", "dedupe_reused"]


def test_concurrent_stale_recovery_records_one_transition(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="web")
    claimed = repo.claim_next(worker_id="worker-a", lease_seconds=1)
    assert claimed and claimed.id == job.id
    expiry = utc_now() + timedelta(seconds=2)

    def recover_once() -> int:
        engine = build_engine(settings)
        return JobRepository(engine, build_session_factory(engine)).recover_expired(now=expiry)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _unused: recover_once(), range(2))) == 1

    with repo.session_factory() as session:
        assert session.get(Job, job.id).status == "waiting"
    assert [event.event_type for event in repo.events_for(job.id)].count("lease_recovered") == 1


def test_concurrent_waiting_activation_records_one_transition(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    job, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="web")
    claimed = repo.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claimed
    assert repo.retry_or_fail(claimed, error_type="Retryable", message="again", delay_seconds=0)
    due = utc_now() + timedelta(seconds=1)

    def activate_once() -> int:
        engine = build_engine(settings)
        return JobRepository(engine, build_session_factory(engine)).activate_due_waiting(now=due)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _unused: activate_once(), range(2))) == 1

    with repo.session_factory() as session:
        assert session.scalar(select(Job.status).where(Job.id == job.id)) == "queued"
    assert [event.event_type for event in repo.events_for(job.id)].count("waiting_activated") == 1


def test_cancellation_is_immediate_for_queued_and_cooperative_for_running(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    queued, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="web")
    assert repo.cancel(job_id=queued.id, actor_source="web") == "cancelled"
    assert repo.events_for(queued.id)[-1].to_status == "cancelled"

    running, _ = repo.enqueue(job_type="system.noop", payload={}, actor_source="web")
    assert repo.claim_next(worker_id="worker", lease_seconds=30)
    assert repo.cancel(job_id=running.id, actor_source="web") == "requested"
    assert repo.events_for(running.id)[-1].event_type == "cancellation_requested"


def test_device_status_poll_schedule_is_idempotent_and_advances_by_configured_interval(settings, monkeypatch) -> None:
    """M7 Fatia 3: the persisted schedule, not wall-clock re-derivation, decides when the next poll is due."""
    repo = repository(settings, monkeypatch)
    t0 = utc_now()

    job, created = repo.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0)
    assert created and job is not None

    # Same instant again: not a new job, and not even a no-op enqueue attempt.
    again, created_again = repo.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0)
    assert created_again is False and again is None

    # One second before the interval elapses: still not due.
    almost, created_almost = repo.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0 + timedelta(minutes=30, seconds=-1))
    assert created_almost is False and almost is None

    # Exactly at the interval: due, and a second, distinct job is created.
    second, created_second = repo.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0 + timedelta(minutes=30))
    assert created_second and second is not None and second.id != job.id
    assert second.payload_json["connection_id"] == 3
    assert second.priority == 150  # below default-100 production/report jobs, per V1's own priority order


def test_device_status_poll_schedule_survives_a_scheduler_restart(settings, monkeypatch) -> None:
    """A fresh JobRepository (simulating a scheduler process restart) reads the
    same persisted cadence a prior instance left behind -- it does not
    re-trigger early, and it does not lose track of what is due."""
    t0 = utc_now()
    first_instance = repository(settings, monkeypatch)
    job, created = first_instance.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0)
    assert created and job is not None

    engine = build_engine(settings)
    restarted_instance = JobRepository(engine, build_session_factory(engine))

    # The "restarted" process, ticking immediately, must not re-fire early.
    immediate, created_immediate = restarted_instance.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0 + timedelta(seconds=1))
    assert created_immediate is False and immediate is None

    # But once its own persisted next_run_at arrives, it fires exactly once.
    due, created_due = restarted_instance.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0 + timedelta(minutes=30))
    assert created_due and due is not None and due.id != job.id


def test_device_status_poll_concurrent_scheduler_ticks_enqueue_one_job(settings, monkeypatch) -> None:
    repository(settings, monkeypatch)
    t0 = utc_now()

    def tick(_unused) -> tuple[int | None, bool]:
        engine = build_engine(settings)
        concurrent_repo = JobRepository(engine, build_session_factory(engine))
        job, created = concurrent_repo.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, now=t0)
        return (job.id if job else None), created

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tick, range(4)))

    created_jobs = {job_id for job_id, created in results if created}
    assert len(created_jobs) == 1
    assert sum(created for _job_id, created in results) == 1


def test_device_status_poll_schedule_requires_a_positive_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    try:
        repo.enqueue_due_device_status_poll(connection_id=3, interval_minutes=0)
        assert False, "must reject a non-positive interval"
    except ValueError:
        pass


def test_device_status_poll_stops_at_its_lifetime_hard_cap(settings, monkeypatch) -> None:
    """An unattended run must not run away just because nobody is watching it."""
    repo = repository(settings, monkeypatch)
    t0 = utc_now()

    first, created_first = repo.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, max_cycles=2, now=t0)
    assert created_first and first is not None

    second, created_second = repo.enqueue_due_device_status_poll(
        connection_id=3, interval_minutes=30, max_cycles=2, now=t0 + timedelta(minutes=30)
    )
    assert created_second and second is not None and second.id != first.id

    # The cap is now reached (2/2): a third due tick must not create a job...
    third, created_third = repo.enqueue_due_device_status_poll(
        connection_id=3, interval_minutes=30, max_cycles=2, now=t0 + timedelta(minutes=60)
    )
    assert created_third is False and third is None

    # ...and must never create one on any later tick either -- the cap is
    # permanent, not a one-time skip that resumes on the next slot.
    fourth, created_fourth = repo.enqueue_due_device_status_poll(
        connection_id=3, interval_minutes=30, max_cycles=2, now=t0 + timedelta(minutes=180)
    )
    assert created_fourth is False and fourth is None

    with repo.session_factory() as session:
        count = session.scalar(
            select(func.count(Job.id)).where(Job.job_type == "device_status.poll")
        )
    assert count == 2


def test_device_status_poll_hard_cap_counts_cycles_from_a_prior_process(settings, monkeypatch) -> None:
    """The cap is read back from persisted state, not a counter private to one process --
    a restarted scheduler must not get a fresh budget."""
    t0 = utc_now()
    first_instance = repository(settings, monkeypatch)
    job, created = first_instance.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, max_cycles=1, now=t0)
    assert created and job is not None

    engine = build_engine(settings)
    restarted_instance = JobRepository(engine, build_session_factory(engine))
    again, created_again = restarted_instance.enqueue_due_device_status_poll(
        connection_id=3, interval_minutes=30, max_cycles=1, now=t0 + timedelta(minutes=30)
    )
    assert created_again is False and again is None


def test_device_status_poll_with_no_cap_configured_is_unbounded(settings, monkeypatch) -> None:
    """`max_cycles=None` (the default) preserves the pre-cap behaviour exactly --
    this is exercised at the `Settings` layer (a real deployment can never reach
    here with polling enabled and no cap; see test_config.py), but the
    repository method itself must not silently assume a cap."""
    repo = repository(settings, monkeypatch)
    t0 = utc_now()
    for cycle in range(3):
        job, created = repo.enqueue_due_device_status_poll(
            connection_id=3, interval_minutes=30, max_cycles=None, now=t0 + timedelta(minutes=30 * cycle)
        )
        assert created and job is not None


def test_incident_evaluation_schedule_is_idempotent_and_advances_by_configured_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    t0 = utc_now()

    job, created = repo.enqueue_due_incident_evaluation(interval_minutes=15, now=t0)
    assert created and job is not None
    assert job.job_type == "diagnostics.evaluate_incidents"

    again, created_again = repo.enqueue_due_incident_evaluation(interval_minutes=15, now=t0)
    assert created_again is False and again is None

    almost, created_almost = repo.enqueue_due_incident_evaluation(interval_minutes=15, now=t0 + timedelta(minutes=15, seconds=-1))
    assert created_almost is False and almost is None

    second, created_second = repo.enqueue_due_incident_evaluation(interval_minutes=15, now=t0 + timedelta(minutes=15))
    assert created_second and second is not None and second.id != job.id


def test_incident_evaluation_schedule_survives_a_restart(settings, monkeypatch) -> None:
    t0 = utc_now()
    first_instance = repository(settings, monkeypatch)
    job, created = first_instance.enqueue_due_incident_evaluation(interval_minutes=15, now=t0)
    assert created and job is not None

    engine = build_engine(settings)
    restarted_instance = JobRepository(engine, build_session_factory(engine))
    immediate, created_immediate = restarted_instance.enqueue_due_incident_evaluation(interval_minutes=15, now=t0 + timedelta(seconds=1))
    assert created_immediate is False and immediate is None

    due, created_due = restarted_instance.enqueue_due_incident_evaluation(interval_minutes=15, now=t0 + timedelta(minutes=15))
    assert created_due and due is not None and due.id != job.id


def test_incident_evaluation_concurrent_ticks_enqueue_one_job(settings, monkeypatch) -> None:
    repository(settings, monkeypatch)
    t0 = utc_now()

    def tick(_unused) -> tuple[int | None, bool]:
        engine = build_engine(settings)
        concurrent_repo = JobRepository(engine, build_session_factory(engine))
        job, created = concurrent_repo.enqueue_due_incident_evaluation(interval_minutes=15, now=t0)
        return (job.id if job else None), created

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tick, range(4)))

    created_jobs = {job_id for job_id, created in results if created}
    assert len(created_jobs) == 1
    assert sum(created for _job_id, created in results) == 1


def test_incident_evaluation_schedule_requires_a_positive_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    try:
        repo.enqueue_due_incident_evaluation(interval_minutes=0)
        assert False, "must reject a non-positive interval"
    except ValueError:
        pass


def test_notification_processing_schedule_is_idempotent_and_advances_by_configured_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    t0 = utc_now()

    job, created = repo.enqueue_due_notification_processing(interval_minutes=15, now=t0)
    assert created and job is not None
    assert job.job_type == "notifications.process"

    again, created_again = repo.enqueue_due_notification_processing(interval_minutes=15, now=t0)
    assert created_again is False and again is None

    second, created_second = repo.enqueue_due_notification_processing(interval_minutes=15, now=t0 + timedelta(minutes=15))
    assert created_second and second is not None and second.id != job.id


def test_notification_processing_schedule_survives_a_restart(settings, monkeypatch) -> None:
    t0 = utc_now()
    first_instance = repository(settings, monkeypatch)
    job, created = first_instance.enqueue_due_notification_processing(interval_minutes=15, now=t0)
    assert created and job is not None

    engine = build_engine(settings)
    restarted_instance = JobRepository(engine, build_session_factory(engine))
    immediate, created_immediate = restarted_instance.enqueue_due_notification_processing(interval_minutes=15, now=t0 + timedelta(seconds=1))
    assert created_immediate is False and immediate is None

    due, created_due = restarted_instance.enqueue_due_notification_processing(interval_minutes=15, now=t0 + timedelta(minutes=15))
    assert created_due and due is not None and due.id != job.id


def test_notification_processing_concurrent_ticks_enqueue_one_job(settings, monkeypatch) -> None:
    repository(settings, monkeypatch)
    t0 = utc_now()

    def tick(_unused) -> tuple[int | None, bool]:
        engine = build_engine(settings)
        concurrent_repo = JobRepository(engine, build_session_factory(engine))
        job, created = concurrent_repo.enqueue_due_notification_processing(interval_minutes=15, now=t0)
        return (job.id if job else None), created

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tick, range(4)))

    created_jobs = {job_id for job_id, created in results if created}
    assert len(created_jobs) == 1
    assert sum(created for _job_id, created in results) == 1


def test_notification_processing_schedule_requires_a_positive_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    try:
        repo.enqueue_due_notification_processing(interval_minutes=0)
        assert False, "must reject a non-positive interval"
    except ValueError:
        pass


def test_digest_generation_schedule_is_idempotent_and_advances_by_configured_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    t0 = utc_now()

    job, created = repo.enqueue_due_digest_generation(interval_minutes=1440, now=t0)
    assert created and job is not None
    assert job.job_type == "digests.generate"

    again, created_again = repo.enqueue_due_digest_generation(interval_minutes=1440, now=t0)
    assert created_again is False and again is None

    second, created_second = repo.enqueue_due_digest_generation(interval_minutes=1440, now=t0 + timedelta(minutes=1440))
    assert created_second and second is not None and second.id != job.id


def test_digest_generation_schedule_survives_a_restart(settings, monkeypatch) -> None:
    t0 = utc_now()
    first_instance = repository(settings, monkeypatch)
    job, created = first_instance.enqueue_due_digest_generation(interval_minutes=1440, now=t0)
    assert created and job is not None

    engine = build_engine(settings)
    restarted_instance = JobRepository(engine, build_session_factory(engine))
    immediate, created_immediate = restarted_instance.enqueue_due_digest_generation(interval_minutes=1440, now=t0 + timedelta(seconds=1))
    assert created_immediate is False and immediate is None

    due, created_due = restarted_instance.enqueue_due_digest_generation(interval_minutes=1440, now=t0 + timedelta(minutes=1440))
    assert created_due and due is not None and due.id != job.id


def test_digest_generation_concurrent_ticks_enqueue_one_job(settings, monkeypatch) -> None:
    repository(settings, monkeypatch)
    t0 = utc_now()

    def tick(_unused) -> tuple[int | None, bool]:
        engine = build_engine(settings)
        concurrent_repo = JobRepository(engine, build_session_factory(engine))
        job, created = concurrent_repo.enqueue_due_digest_generation(interval_minutes=1440, now=t0)
        return (job.id if job else None), created

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tick, range(4)))

    created_jobs = {job_id for job_id, created in results if created}
    assert len(created_jobs) == 1
    assert sum(created for _job_id, created in results) == 1


def test_digest_generation_schedule_requires_a_positive_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    try:
        repo.enqueue_due_digest_generation(interval_minutes=0)
        assert False, "must reject a non-positive interval"
    except ValueError:
        pass


# --- Telegram O&M redesign, Fatia 4: recovery digest + morning briefing --------


def test_recovery_digest_schedule_carries_its_kind_and_chains_like_the_diagnostics_digest(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    t0 = utc_now()

    job, created = repo.enqueue_due_recovery_digest(interval_minutes=120, now=t0)
    assert created and job is not None
    assert job.job_type == "digests.generate"
    assert job.payload_json["kind"] == "recoveries"

    again, created_again = repo.enqueue_due_recovery_digest(interval_minutes=120, now=t0)
    assert created_again is False and again is None

    second, created_second = repo.enqueue_due_recovery_digest(interval_minutes=120, now=t0 + timedelta(minutes=120))
    assert created_second and second is not None and second.id != job.id


def test_morning_briefing_first_activation_anchors_to_the_next_local_9am(settings, monkeypatch) -> None:
    """Req 10-11's central scheduling rule: turning this on mid-afternoon must
    not fire a briefing mid-afternoon -- the very first slot is the *next*
    09:00 Europe/Lisbon, in UTC, never "now"."""
    from datetime import datetime, timezone

    repo = repository(settings, monkeypatch)
    afternoon = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)  # 14:00 in Lisbon summer time (UTC+1)

    job, created = repo.enqueue_due_morning_briefing(now=afternoon)
    assert created is False and job is None  # not due yet -- 09:00 tomorrow local, not this afternoon

    next_morning = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)  # 09:00 Lisbon summer time
    due_job, due_created = repo.enqueue_due_morning_briefing(now=next_morning)
    assert due_created and due_job is not None
    assert due_job.job_type == "digests.generate"
    assert due_job.payload_json["kind"] == "morning_briefing"


def test_morning_briefing_schedule_is_idempotent_and_advances_daily(settings, monkeypatch) -> None:
    from datetime import datetime, timezone

    repo = repository(settings, monkeypatch)
    at_9am = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)  # 09:00 Lisbon

    job, created = repo.enqueue_due_morning_briefing(now=at_9am)
    assert created and job is not None

    again, created_again = repo.enqueue_due_morning_briefing(now=at_9am)
    assert created_again is False and again is None

    next_day = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
    second, created_second = repo.enqueue_due_morning_briefing(now=next_day)
    assert created_second and second is not None and second.id != job.id


def test_morning_briefing_uses_the_configured_hour_and_timezone(settings, monkeypatch) -> None:
    from datetime import datetime, timezone

    repo = repository(settings, monkeypatch)
    # Requesting 07:00 UTC (a fixed-offset zone, to avoid DST ambiguity in
    # the test itself): 06:59 UTC is not yet due, 07:00 UTC is.
    not_yet = datetime(2026, 7, 24, 6, 59, tzinfo=timezone.utc)
    job, created = repo.enqueue_due_morning_briefing(now=not_yet, hour=7, minute=0, tz_name="UTC")
    assert created is False and job is None

    due = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
    due_job, due_created = repo.enqueue_due_morning_briefing(now=due, hour=7, minute=0, tz_name="UTC")
    assert due_created and due_job is not None


def test_production_backfill_progress_is_persisted_before_reschedule(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    job, _ = repo.enqueue(
        job_type="production.bounded_backfill",
        payload={"mode": "bounded_backfill", "connection_id": 1, "start_date": "2026-01-01", "end_date": "2026-01-31"},
        actor_source="web",
    )
    claimed = repo.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claimed is not None
    assert repo.reschedule(claimed, payload={**claimed.payload, "next_source_day": "2026-01-11"})
    with repo.session_factory() as session:
        persisted = session.get(Job, job.id)
        assert persisted is not None and persisted.status == "waiting"
        assert persisted.payload_json["next_source_day"] == "2026-01-11"
    assert repo.events_for(job.id)[-1].event_type == "progress_saved"


def test_production_sync_schedule_is_idempotent_and_advances_by_configured_interval(settings, monkeypatch) -> None:
    """Same restart-safe, persisted-schedule cadence as device-status polling,
    for `production.incremental` -- no lifetime cap, this one runs forever
    once turned on for a connection, matching V1's own daily production
    sync."""
    repo = repository(settings, monkeypatch)
    t0 = utc_now()

    job, created = repo.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0)
    assert created and job is not None
    assert job.payload_json == {"connection_id": 3, "scheduled_for": t0.isoformat()}
    assert job.priority == 100

    again, created_again = repo.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0)
    assert created_again is False and again is None

    almost, created_almost = repo.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0 + timedelta(hours=24, seconds=-1))
    assert created_almost is False and almost is None

    second, created_second = repo.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0 + timedelta(hours=24))
    assert created_second and second is not None and second.id != job.id


def test_production_sync_schedule_survives_a_scheduler_restart(settings, monkeypatch) -> None:
    t0 = utc_now()
    first_instance = repository(settings, monkeypatch)
    job, created = first_instance.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0)
    assert created and job is not None

    engine = build_engine(settings)
    restarted_instance = JobRepository(engine, build_session_factory(engine))

    immediate, created_immediate = restarted_instance.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0 + timedelta(seconds=1))
    assert created_immediate is False and immediate is None

    due, created_due = restarted_instance.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0 + timedelta(hours=24))
    assert created_due and due is not None and due.id != job.id


def test_production_sync_concurrent_scheduler_ticks_enqueue_one_job(settings, monkeypatch) -> None:
    repository(settings, monkeypatch)
    t0 = utc_now()

    def tick(_unused) -> tuple[int | None, bool]:
        engine = build_engine(settings)
        concurrent_repo = JobRepository(engine, build_session_factory(engine))
        job, created = concurrent_repo.enqueue_due_production_incremental(connection_id=3, interval_hours=24, now=t0)
        return (job.id if job else None), created

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tick, range(4)))

    created_jobs = {job_id for job_id, created in results if created}
    assert len(created_jobs) == 1
    assert sum(created for _job_id, created in results) == 1


def test_production_sync_schedule_requires_a_positive_interval(settings, monkeypatch) -> None:
    repo = repository(settings, monkeypatch)
    try:
        repo.enqueue_due_production_incremental(connection_id=3, interval_hours=0)
        assert False, "must reject a non-positive interval"
    except ValueError:
        pass
