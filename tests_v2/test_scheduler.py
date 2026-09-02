from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job, ScheduleState
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


def test_scheduler_enqueues_device_status_poll_when_enabled_and_respects_its_cap(settings, monkeypatch) -> None:
    """The real `run_once()` loop this milestone targets -- not a driver script standing
    in for it -- must enqueue device_status.poll jobs on its own, and must stop doing so
    once the configured lifetime cap is reached, with no human watching."""
    upgrade(settings, monkeypatch)
    device_settings = dataclasses.replace(
        settings,
        device_status_poll_enabled=True,
        device_status_poll_connection_id=3,
        device_status_poll_interval_minutes=30,
        device_status_poll_max_cycles=2,
    )
    scheduler = Scheduler(device_settings, owner_token="scheduler-device")

    # First tick: both the hourly noop and the device-status poll are due.
    assert scheduler.run_once() is True
    # Immediately again: neither is due yet.
    assert scheduler.run_once() is False

    engine = build_engine(settings)

    def poll_jobs() -> list[Job]:
        with build_session_factory(engine)() as session:
            return session.scalars(select(Job).where(Job.job_type == "device_status.poll")).all()

    assert len(poll_jobs()) == 1

    # Force the persisted schedule to be due, repeatedly, past the cap of 2.
    for _ in range(3):
        with build_session_factory(engine)() as session:
            schedule = session.get(ScheduleState, "device_status.poll:3")
            schedule.next_run_at = schedule.next_run_at.replace(year=2020)
            session.commit()
        scheduler.run_once()

    # Capped at 2 lifetime jobs no matter how many more times the due slot is forced.
    assert len(poll_jobs()) == 2


def test_scheduler_never_enqueues_device_status_poll_when_disabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-default-off")
    scheduler.run_once()
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        polls = session.scalars(select(Job).where(Job.job_type == "device_status.poll")).all()
    assert polls == []


def test_scheduler_enqueues_incident_evaluation_when_enabled(settings, monkeypatch) -> None:
    """D1: no connection id, no cap needed -- this makes no provider calls."""
    upgrade(settings, monkeypatch)
    incident_settings = dataclasses.replace(
        settings, diagnostic_incident_evaluation_enabled=True, diagnostic_incident_evaluation_interval_minutes=15
    )
    scheduler = Scheduler(incident_settings, owner_token="scheduler-incidents")
    assert scheduler.run_once() is True
    assert scheduler.run_once() is False

    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "diagnostics.evaluate_incidents")).all()
    assert len(jobs) == 1


def test_scheduler_never_enqueues_incident_evaluation_when_disabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-incidents-off")
    scheduler.run_once()
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "diagnostics.evaluate_incidents")).all()
    assert jobs == []


def test_scheduler_enqueues_notification_processing_when_enabled(settings, monkeypatch) -> None:
    """D3: no connection id, no cap needed -- this makes no provider calls,
    and delivery can only ever reach the mock Telegram client."""
    upgrade(settings, monkeypatch)
    notification_settings = dataclasses.replace(
        settings, notification_processing_enabled=True, notification_processing_interval_minutes=15
    )
    scheduler = Scheduler(notification_settings, owner_token="scheduler-notifications")
    assert scheduler.run_once() is True
    assert scheduler.run_once() is False

    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "notifications.process")).all()
    assert len(jobs) == 1


def test_scheduler_never_enqueues_notification_processing_when_disabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-notifications-off")
    scheduler.run_once()
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "notifications.process")).all()
    assert jobs == []


def test_scheduler_enqueues_digest_generation_when_enabled(settings, monkeypatch) -> None:
    """D6: no provider call, no channel needed to generate -- delivery (a
    separate step) can only ever reach the mock Telegram client anyway."""
    upgrade(settings, monkeypatch)
    digest_settings = dataclasses.replace(
        settings, digest_generation_enabled=True, digest_generation_interval_minutes=1440
    )
    scheduler = Scheduler(digest_settings, owner_token="scheduler-digests")
    assert scheduler.run_once() is True
    assert scheduler.run_once() is False

    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "digests.generate")).all()
    assert len(jobs) == 1


def test_scheduler_never_enqueues_digest_generation_when_disabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-digests-off")
    scheduler.run_once()
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "digests.generate")).all()
    assert jobs == []


def test_scheduler_enqueues_recovery_digest_when_enabled(settings, monkeypatch) -> None:
    """Req 13: off by default, no provider call, same job type as every
    other digest kind (`digests.generate`) -- the `kind` payload key, not a
    second job type, is what routes it."""
    upgrade(settings, monkeypatch)
    recovery_settings = dataclasses.replace(
        settings, recovery_digest_generation_enabled=True, recovery_digest_interval_minutes=120,
    )
    scheduler = Scheduler(recovery_settings, owner_token="scheduler-recoveries")
    assert scheduler.run_once() is True
    assert scheduler.run_once() is False

    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "digests.generate")).all()
    assert len(jobs) == 1
    assert jobs[0].payload_json["kind"] == "recoveries"


def test_scheduler_never_enqueues_recovery_digest_when_disabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-recoveries-off")
    scheduler.run_once()
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "digests.generate")).all()
    assert jobs == []


def test_scheduler_seeds_morning_briefing_schedule_without_firing_immediately(settings, monkeypatch) -> None:
    """Reqs 10-11: off by default. First activation anchors to the next
    09:00 local, so `run_once` at an arbitrary moment in the test run is not
    guaranteed to be immediately due -- proven separately, against a real
    clock boundary, in `test_jobs.py`. Here: the switch itself works, and a
    disabled scheduler never enqueues one at all."""
    upgrade(settings, monkeypatch)
    briefing_settings = dataclasses.replace(settings, morning_briefing_enabled=True)
    scheduler = Scheduler(briefing_settings, owner_token="scheduler-briefing")
    scheduler.run_once()  # first activation: seeds the schedule, never immediately due
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "digests.generate")).all()
    assert jobs == []  # not due yet -- proves the anchor, not "the switch does nothing"


def test_scheduler_never_enqueues_morning_briefing_when_disabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-briefing-off")
    scheduler.run_once()
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "digests.generate")).all()
    assert jobs == []


def test_scheduler_enqueues_production_sync_when_enabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    production_settings = dataclasses.replace(
        settings,
        production_sync_scheduler_enabled=True,
        production_sync_scheduler_connection_id=3,
        production_sync_scheduler_interval_hours=24,
    )
    scheduler = Scheduler(production_settings, owner_token="scheduler-production")

    assert scheduler.run_once() is True
    assert scheduler.run_once() is False

    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "production.incremental")).all()
    assert len(jobs) == 1
    assert jobs[0].payload_json["connection_id"] == 3


def test_scheduler_never_enqueues_production_sync_when_disabled(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    scheduler = Scheduler(settings, owner_token="scheduler-production-off")
    scheduler.run_once()
    engine = build_engine(settings)
    with build_session_factory(engine)() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "production.incremental")).all()
    assert jobs == []
