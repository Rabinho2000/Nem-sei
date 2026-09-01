"""The productive window, checked against published solar times and edge cases.

The accuracy tests use Lisbon and Faro on the solstices and an equinox, with
sunrise/sunset values from a published almanac converted to UTC. A two-minute
tolerance is far tighter than the 45-minute margin the rules apply, so an
error large enough to fail here could never change a rule's verdict -- which
is the point: this pins the arithmetic, not the policy.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nemsei.monitoring.production_window import (
    DEFAULT_MARGIN,
    WINDOW_STATES,
    sun_times,
    window_for,
)


LISBON = (38.7223, -9.1393)
FARO = (37.0194, -7.9322)
TOLERANCE = timedelta(minutes=2)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "latitude,longitude,day,expected_sunrise,expected_sunset",
    [
        # Lisbon, June solstice: 06:12 / 21:04 WEST = 05:12 / 20:04 UTC.
        (*LISBON, date(2026, 6, 21), utc(2026, 6, 21, 5, 12), utc(2026, 6, 21, 20, 4)),
        # Lisbon, December solstice: 07:51 / 17:18 WET = the same in UTC.
        (*LISBON, date(2026, 12, 21), utc(2026, 12, 21, 7, 51), utc(2026, 12, 21, 17, 18)),
        # Faro, March equinox.
        (*FARO, date(2026, 3, 20), utc(2026, 3, 20, 6, 34), utc(2026, 3, 20, 18, 42)),
    ],
)
def test_sun_times_match_published_values(
    latitude: float, longitude: float, day: date, expected_sunrise: datetime, expected_sunset: datetime
) -> None:
    sunrise, sunset = sun_times(latitude=latitude, longitude=longitude, on=day)
    assert sunrise is not None and sunset is not None
    assert abs(sunrise - expected_sunrise) < TOLERANCE
    assert abs(sunset - expected_sunset) < TOLERANCE


def test_a_summer_day_is_longer_than_a_winter_day() -> None:
    summer = sun_times(latitude=LISBON[0], longitude=LISBON[1], on=date(2026, 6, 21))
    winter = sun_times(latitude=LISBON[0], longitude=LISBON[1], on=date(2026, 12, 21))
    assert (summer[1] - summer[0]) > (winter[1] - winter[0]) + timedelta(hours=4)


def test_three_in_the_morning_is_never_productive() -> None:
    """The whole reason this module exists: an inverter at rest at night is
    not a fault, and a rule that cannot see that opens one incident per plant
    per night."""
    window = window_for(latitude=LISBON[0], longitude=LISBON[1], at=utc(2026, 6, 21, 3, 0))
    assert window.state == "dark"
    assert not window.is_productive


def test_midday_in_june_is_productive() -> None:
    window = window_for(latitude=LISBON[0], longitude=LISBON[1], at=utc(2026, 6, 21, 12, 0))
    assert window.state == "productive"
    assert window.is_productive


def test_midday_in_december_is_still_productive() -> None:
    """A short winter day must not collapse into nothing."""
    window = window_for(latitude=LISBON[0], longitude=LISBON[1], at=utc(2026, 12, 21, 12, 30))
    assert window.state == "productive"


def test_the_margin_keeps_the_minutes_around_sunrise_out_of_the_window() -> None:
    """Sunrise is not the moment a plant starts working."""
    sunrise, _ = sun_times(latitude=LISBON[0], longitude=LISBON[1], on=date(2026, 6, 21))
    just_after = window_for(latitude=LISBON[0], longitude=LISBON[1], at=sunrise + timedelta(minutes=5))
    well_after = window_for(latitude=LISBON[0], longitude=LISBON[1], at=sunrise + DEFAULT_MARGIN + timedelta(minutes=5))
    assert just_after.state == "dark"
    assert well_after.state == "productive"


def test_the_margin_is_adjustable_without_touching_a_rule() -> None:
    sunrise, _ = sun_times(latitude=LISBON[0], longitude=LISBON[1], on=date(2026, 6, 21))
    moment = sunrise + timedelta(minutes=20)
    assert window_for(latitude=LISBON[0], longitude=LISBON[1], at=moment).state == "dark"
    assert (
        window_for(latitude=LISBON[0], longitude=LISBON[1], at=moment, margin=timedelta(minutes=5)).state
        == "productive"
    )


def test_no_coordinates_is_unknown_and_never_silently_dark() -> None:
    """267 installations have no coordinates today. Answering `dark` for them
    would suppress every production rule around the clock and look like
    nothing was wrong."""
    window = window_for(latitude=None, longitude=None, at=utc(2026, 6, 21, 12, 0))
    assert window.state == "unknown"
    assert not window.is_known
    assert not window.is_productive
    assert window.reason is not None


def test_one_missing_coordinate_is_as_unusable_as_both() -> None:
    assert window_for(latitude=Decimal("38.7"), longitude=None, at=utc(2026, 6, 21, 12, 0)).state == "unknown"
    assert window_for(latitude=None, longitude=Decimal("-9.1"), at=utc(2026, 6, 21, 12, 0)).state == "unknown"


def test_decimal_coordinates_are_accepted_unchanged() -> None:
    """The column type is Numeric(10, 7); floats must not be required."""
    from_decimal = window_for(latitude=Decimal("38.7223"), longitude=Decimal("-9.1393"), at=utc(2026, 6, 21, 12, 0))
    from_float = window_for(latitude=38.7223, longitude=-9.1393, at=utc(2026, 6, 21, 12, 0))
    assert from_decimal.state == from_float.state == "productive"
    assert from_decimal.starts_at == from_float.starts_at


def test_a_naive_datetime_is_refused_rather_than_assumed_to_be_utc() -> None:
    with pytest.raises(ValueError):
        window_for(latitude=LISBON[0], longitude=LISBON[1], at=datetime(2026, 6, 21, 12, 0))


def test_a_local_time_is_converted_not_reinterpreted() -> None:
    """13:00 in Lisbon summer time is 12:00 UTC and must give the same answer."""
    lisbon_summer = timezone(timedelta(hours=1))
    local = datetime(2026, 6, 21, 13, 0, tzinfo=lisbon_summer)
    assert window_for(latitude=LISBON[0], longitude=LISBON[1], at=local).state == "productive"
    assert window_for(latitude=LISBON[0], longitude=LISBON[1], at=local).starts_at == window_for(
        latitude=LISBON[0], longitude=LISBON[1], at=utc(2026, 6, 21, 12, 0)
    ).starts_at


def test_polar_night_is_dark_all_day() -> None:
    sunrise, sunset = sun_times(latitude=78.0, longitude=15.0, on=date(2026, 12, 21))
    assert sunrise is None and sunset is None
    assert window_for(latitude=78.0, longitude=15.0, at=utc(2026, 12, 21, 12, 0)).state == "dark"


def test_polar_day_is_productive_at_midnight() -> None:
    """The case a latitude-based guess gets backwards."""
    assert window_for(latitude=78.0, longitude=15.0, at=utc(2026, 6, 21, 0, 0)).state == "productive"
    assert window_for(latitude=78.0, longitude=15.0, at=utc(2026, 6, 21, 12, 0)).state == "productive"


def test_southern_hemisphere_seasons_are_the_other_way_round() -> None:
    """A sign error in the declination would pass every Portuguese test."""
    sydney = (-33.8688, 151.2093)
    december = sun_times(latitude=sydney[0], longitude=sydney[1], on=date(2026, 12, 21))
    june = sun_times(latitude=sydney[0], longitude=sydney[1], on=date(2026, 6, 21))
    assert (december[1] - december[0]) > (june[1] - june[0])


def test_the_equator_has_roughly_twelve_hour_days_all_year() -> None:
    for day in (date(2026, 3, 20), date(2026, 6, 21), date(2026, 12, 21)):
        sunrise, sunset = sun_times(latitude=0.0, longitude=0.0, on=day)
        assert abs((sunset - sunrise) - timedelta(hours=12)) < timedelta(minutes=10)


def test_longitude_shifts_the_window_but_not_its_length() -> None:
    """Same latitude, 15 degrees east: an hour earlier in UTC, same duration."""
    west = sun_times(latitude=38.7, longitude=0.0, on=date(2026, 6, 21))
    east = sun_times(latitude=38.7, longitude=15.0, on=date(2026, 6, 21))
    assert abs((west[0] - east[0]) - timedelta(hours=1)) < timedelta(minutes=1)
    assert abs((west[1] - west[0]) - (east[1] - east[0])) < timedelta(minutes=1)


def test_every_state_returned_is_in_the_declared_vocabulary() -> None:
    cases = [
        window_for(latitude=None, longitude=None, at=utc(2026, 6, 21, 12)),
        window_for(latitude=LISBON[0], longitude=LISBON[1], at=utc(2026, 6, 21, 12)),
        window_for(latitude=LISBON[0], longitude=LISBON[1], at=utc(2026, 6, 21, 3)),
        window_for(latitude=78.0, longitude=15.0, at=utc(2026, 12, 21, 12)),
    ]
    assert {window.state for window in cases} <= set(WINDOW_STATES)
