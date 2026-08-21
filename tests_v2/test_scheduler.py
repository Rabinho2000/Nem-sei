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
