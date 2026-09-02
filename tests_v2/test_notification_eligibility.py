"""Pure eligibility gates (Telegram O&M redesign, reqs 2-4/14/17) -- no
database, no Telegram, direct function calls against `NotificationEpisode`
dataclass-like rows built in memory.
"""
from __future__ import annotations

from datetime import datetime, timezone

from nemsei.notifications.eligibility import (
    eligible_for_recovery_digest,
    is_silent_period,
    next_reminder_due,
    should_notify_open,
    should_notify_recovery_immediately,
)
from nemsei.notifications.models import NotificationEpisode


def utc(hour: int = 9, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def episode(**overrides) -> NotificationEpisode:
    defaults = dict(
        asset_id=1, device_id=None, problem_family="communication", status="open", severity_peak="critical",
        opened_at=utc(9), last_activity_at=utc(9), closed_at=None, flap_count=1, first_incident_id=1,
        last_incident_id=1, eligible_at=None, notified_at=None, last_reminder_at=None, reminder_count=0,
        recovery_notified=False, created_at=utc(9), updated_at=utc(9),
    )
    defaults.update(overrides)
    return NotificationEpisode(**defaults)


# --- silence / open eligibility --------------------------------------------------


def test_is_silent_period_true_before_eligible_at_is_set() -> None:
    assert is_silent_period(episode(eligible_at=None)) is True


def test_is_silent_period_false_once_eligible() -> None:
    assert is_silent_period(episode(eligible_at=utc(9, 30))) is False


def test_should_notify_open_false_while_silent() -> None:
    assert should_notify_open(episode(eligible_at=None)) is False


def test_should_notify_open_true_once_eligible_and_not_yet_notified() -> None:
    assert should_notify_open(episode(eligible_at=utc(9, 30), notified_at=None)) is True


def test_should_notify_open_false_once_already_notified() -> None:
    """This is the actual flap-storm guard: an episode a flap keeps
    reopening never clears `notified_at`, so it is never "still owed" again."""
    assert should_notify_open(episode(eligible_at=utc(9, 30), notified_at=utc(9, 31))) is False


# --- recovery: the hard floor and the immediate/digest split -------------------


def test_recovery_never_sent_for_an_episode_that_was_never_notified() -> None:
    ep = episode(status="closed", closed_at=utc(20), notified_at=None, severity_peak="critical")
    assert should_notify_recovery_immediately(ep) is False
    assert eligible_for_recovery_digest(ep) is False  # not even the slower channel


def test_recovery_is_immediate_for_a_notified_critical_episode() -> None:
    ep = episode(status="closed", closed_at=utc(11), notified_at=utc(9, 31), severity_peak="critical")
    assert should_notify_recovery_immediately(ep) is True
    assert eligible_for_recovery_digest(ep) is False  # never both


def test_recovery_is_immediate_when_a_reminder_already_went_out() -> None:
    ep = episode(
        status="closed", closed_at=utc(15), notified_at=utc(9, 31), severity_peak="warning", reminder_count=1,
    )
    assert should_notify_recovery_immediately(ep) is True


def test_recovery_is_immediate_for_a_long_but_non_critical_episode() -> None:
    ep = episode(
        status="closed", opened_at=utc(9), closed_at=utc(14), notified_at=utc(9, 31), severity_peak="warning",
    )
    assert should_notify_recovery_immediately(ep, significant_duration_minutes=240) is True


def test_recovery_of_a_short_non_critical_notified_episode_goes_to_the_digest() -> None:
    ep = episode(
        status="closed", opened_at=utc(9), closed_at=utc(9, 45), notified_at=utc(9, 31), severity_peak="warning",
    )
    assert should_notify_recovery_immediately(ep, significant_duration_minutes=240) is False
    assert eligible_for_recovery_digest(ep) is True


# --- reminder cadence ------------------------------------------------------------


def test_reminder_not_due_before_the_first_tier() -> None:
    ep = episode(notified_at=utc(9, 31), severity_peak="critical")
    decision = next_reminder_due(ep, now=utc(12, 0), reminder_minutes=(240, 1440))
    assert decision.due is False and decision.threshold_minutes == 240


def test_reminder_due_once_the_first_tier_is_crossed() -> None:
    ep = episode(opened_at=utc(9, 0), notified_at=utc(9, 31), severity_peak="critical", reminder_count=0)
    decision = next_reminder_due(ep, now=utc(13, 5), reminder_minutes=(240, 1440))
    assert decision.due is True and decision.threshold_minutes == 240


def test_reminder_moves_to_the_second_tier_after_the_first_was_sent() -> None:
    ep = episode(
        opened_at=utc(9, 0), notified_at=utc(9, 31), severity_peak="critical", reminder_count=1,
        last_reminder_at=utc(13, 5),
    )
    too_soon = next_reminder_due(ep, now=utc(20, 0), reminder_minutes=(240, 1440))
    assert too_soon.due is False and too_soon.threshold_minutes == 1440

    due = next_reminder_due(ep, now=utc(9, 5, day=25), reminder_minutes=(240, 1440))  # opened_at + 24h05
    assert due.due is True and due.threshold_minutes == 1440


def test_reminder_never_fires_once_every_tier_is_exhausted() -> None:
    ep = episode(opened_at=utc(9, 0), notified_at=utc(9, 31), severity_peak="critical", reminder_count=2)
    decision = next_reminder_due(ep, now=utc(9, 0, day=30), reminder_minutes=(240, 1440))
    assert decision.due is False


def test_reminder_never_fires_for_a_non_critical_episode() -> None:
    ep = episode(opened_at=utc(9, 0), notified_at=utc(9, 31), severity_peak="warning")
    decision = next_reminder_due(ep, now=utc(15, 0), reminder_minutes=(240, 1440))
    assert decision.due is False
    assert "critical" in decision.reason


def test_a_reminder_never_fires_for_an_episode_never_notified() -> None:
    ep = episode(opened_at=utc(9, 0), notified_at=None, severity_peak="critical")
    decision = next_reminder_due(ep, now=utc(15, 0), reminder_minutes=(240, 1440))
    assert decision.due is False


def test_planned_work_suppresses_the_reminder_cadence() -> None:
    ep = episode(opened_at=utc(9, 0), notified_at=utc(9, 31), severity_peak="critical")
    decision = next_reminder_due(ep, now=utc(13, 5), has_active_work=True, reminder_minutes=(240, 1440))
    assert decision.due is False
    assert "work" in decision.reason
