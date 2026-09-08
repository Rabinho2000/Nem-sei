"""What Sigenergy is asked, and how often, must not have changed.

ING-001 moved where the writes commit. It was not allowed to move a single
provider call, and "not allowed" is worth an assertion rather than an
intention: the restructure buffers the whole window in memory before opening
a transaction, and buffering is exactly the kind of change that quietly turns
one request per day into one request per retry.

These assertions were written to pass both before and after the conversion.
Running this file against the parent commit is the check that they describe
the old behaviour and not merely the new one.
"""
from __future__ import annotations

from datetime import date

from nemsei.config import Settings
from tests_v2.test_reliability_regressions import (
    COMPLETE_DAY,
    RecordingClient,
    sigenergy_fixture,
)


class CountingClient(RecordingClient):
    """RecordingClient plus a count of logins."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.authentications = 0

    def authenticate(self) -> None:
        self.authentications += 1
        return None


def _service(factory, stub):
    from nemsei.integrations.sigenergy.production import SigenergyProductionService

    return SigenergyProductionService(
        factory,
        Settings.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )


def test_one_login_per_run_whatever_the_window(settings, monkeypatch):
    """The login is per run, not per day and not per mapping."""
    factory, _asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    stub = CountingClient()

    _service(factory, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 20)
    )

    assert stub.authentications == 1


def test_one_request_per_mapping_day_and_no_more(settings, monkeypatch):
    """Three days, one mapping: three requests, in ascending order, once each."""
    factory, _asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    stub = CountingClient()

    _service(factory, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 20)
    )

    assert stub.requested == [
        ("SYS1", date(2026, 8, 18)),
        ("SYS1", date(2026, 8, 19)),
        ("SYS1", date(2026, 8, 20)),
    ]


def test_a_failed_day_is_not_retried_inside_the_same_run(settings, monkeypatch):
    """A day that came back empty costs exactly one call, not two.

    Buffering the window could easily have introduced a second pass over the
    days that failed to persist. It did not.
    """
    factory, _asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    stub = CountingClient(by_day={date(2026, 8, 19): {"unit": "kWh"}})

    _service(factory, stub).sync_daily_production(
        connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 20)
    )

    assert stub.requested.count(("SYS1", date(2026, 8, 19))) == 1
    assert len(stub.requested) == 3


def test_a_window_with_nothing_due_costs_no_call_at_all(settings, monkeypatch):
    """Not even the login. The window is decided before the client is built."""
    factory, _asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    stub = CountingClient()
    from nemsei.shared.clock import utc_now
    from zoneinfo import ZoneInfo

    today = utc_now().astimezone(ZoneInfo("Europe/Lisbon")).date()

    _service(factory, stub).sync_daily_production(
        connection_id, start_date=today, end_date=today
    )

    assert stub.authentications == 0
    assert stub.requested == []


def test_the_incremental_window_is_still_bounded_by_max_days(settings, monkeypatch):
    """The chunking bound is unchanged: at most `max_days` days per run."""
    factory, _asset_id, connection_id, _mapping = sigenergy_fixture(settings, monkeypatch)
    stub = CountingClient()

    _service(factory, stub).sync_incremental(connection_id, max_days=3)

    assert len(stub.requested) <= 3
