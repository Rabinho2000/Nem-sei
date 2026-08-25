"""A schedule left behind catches up; it does not replay the backlog.

On 2026-08-25 the device-status schedule had sat at 2026-08-20 for five days.
Every scheduler tick advanced `next_run_at` by one interval and enqueued a real
provider call for the slot it had just passed, so it began working through ~250
missed slots one per tick: eighty cycles and 165 provider calls in five minutes
before it was stopped. This is the regression test for that.
"""
from __future__ import annotations

from datetime import timedelta

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job, ScheduleState
from nemsei.jobs.repository import JobRepository, _catch_up_slot
from nemsei.shared.clock import utc_now
from tests_v2.test_migrations import upgrade


def test_a_slot_within_one_interval_is_kept_so_the_cadence_does_not_drift() -> None:
    now = utc_now()
    slot = now - timedelta(minutes=20)

    assert _catch_up_slot(slot, now=now, interval=timedelta(minutes=30)) == slot


def test_a_slot_further_back_than_one_interval_jumps_to_now() -> None:
    now = utc_now()
    stale = now - timedelta(days=5)

    assert _catch_up_slot(stale, now=now, interval=timedelta(minutes=30)) == now


def test_a_five_day_outage_enqueues_one_cycle_not_a_backlog(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    repository = JobRepository(engine, factory)
    now = utc_now()
    with factory() as session:
        session.add(ScheduleState(schedule_key="device_status.poll:3", next_run_at=now - timedelta(days=5), updated_at=now))
        session.commit()

    # Ten ticks in quick succession, as the real scheduler would do.
    created = 0
    for _ in range(10):
        _job, made = repository.enqueue_due_device_status_poll(connection_id=3, interval_minutes=30, max_cycles=1000)
        created += 1 if made else 0

    assert created == 1, "a backlog was replayed instead of skipped"
    with factory() as session:
        assert session.query(Job).filter(Job.job_type == "device_status.poll").count() == 1
        schedule = session.get(ScheduleState, "device_status.poll:3")
        # And the schedule now sits one interval ahead of now, not of 2026-08-20.
        assert schedule.next_run_at > now


def test_normal_operation_still_fires_each_due_slot(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    repository = JobRepository(engine, factory)
    now = utc_now()
    with factory() as session:
        session.add(ScheduleState(schedule_key="device_status.poll:7", next_run_at=now - timedelta(minutes=1), updated_at=now))
        session.commit()

    _job, first = repository.enqueue_due_device_status_poll(connection_id=7, interval_minutes=30, max_cycles=1000)
    _job, second = repository.enqueue_due_device_status_poll(connection_id=7, interval_minutes=30, max_cycles=1000)

    assert first is True, "a due slot must still fire"
    assert second is False, "the next slot is not due yet"
