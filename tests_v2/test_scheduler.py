from __future__ import annotations

from nemsei.jobs.scheduler import Scheduler
from tests_v2.test_migrations import upgrade


def test_scheduler_only_enqueues_a_noop_once_per_slot(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-a")
    assert scheduler.run_once() is True
    assert scheduler.run_once() is False
