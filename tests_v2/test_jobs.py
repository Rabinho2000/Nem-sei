from __future__ import annotations

from nemsei.db import build_engine, build_session_factory
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
