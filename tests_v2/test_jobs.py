from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.repository import JobRepository
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
