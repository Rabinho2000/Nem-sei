from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.repository import JobRepository
from nemsei.jobs.scheduler import Scheduler
from tests_v2.test_migrations import upgrade


def test_scheduler_only_enqueues_a_noop_once_per_slot(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-a")
    assert scheduler.run_once() is True
    assert scheduler.run_once() is False


def test_concurrent_schedulers_have_one_valid_lease_owner(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)

    def acquire(owner: str) -> bool:
        engine = build_engine(settings)
        return JobRepository(engine, build_session_factory(engine)).acquire_scheduler_lease(
            owner_token=owner, lease_seconds=30
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(pool.map(acquire, ("scheduler-a", "scheduler-b")))
    assert owners.count(True) == 1
