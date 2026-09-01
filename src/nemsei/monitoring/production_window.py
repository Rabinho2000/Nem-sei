"""When an installation could plausibly be generating, and when it could not.

Zero production at three in the morning is not a fault, and a rule that cannot
tell the difference produces one incident per plant per night. Every
production-absence rule has to ask this module first.

The answer is deliberately three-valued, never a boolean:

    productive  the sun is up here, so nothing generated is worth explaining
    dark        the sun is down here, so nothing generated is expected
    unknown     nobody recorded where this plant is

`unknown` is not a disguised `dark`. It is the honest answer for an
installation with no coordinates, and it is a *visible* one: a plant that
cannot be evaluated must show up as a plant that cannot be evaluated, not
silently drop out of monitoring. See `KNOWN_GAPS.md` and the finding that
`diagnostics.findings` raises for it.

Sunrise and sunset are computed from the NOAA solar position algorithm rather
than pulled from a dependency. It is forty lines of arithmetic, it needs no
network, no data file and no package, and it is accurate to well under a
minute at these latitudes -- far inside the margin below. Adding a dependency
for this would fail `GOAL.md` §23's "every dependency needs a reason".

The margin exists because sunrise is not the moment a plant starts working. At
the horizon the sun delivers almost no usable irradiance, the inverters are
still waking, and the panels may be shaded by anything at all. A plant
producing nothing four minutes after sunrise is normal; four hours after
sunrise it is not. The default 45 minutes is a judgement, not a measurement,
which is why it is a parameter and not a literal buried in a rule.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal


# Solar zenith at apparent sunrise/sunset: 90 degrees of geometry plus 50
# arcminutes for atmospheric refraction and the sun's own radius. This is the
# standard "official" sunrise, the same one an almanac prints.
SUNRISE_ZENITH_DEGREES = 90.833

# How far inside sunrise and sunset the productive window starts and ends.
# Not derived from irradiance data -- V2 holds none -- so it is stated as the
# judgement it is, and kept adjustable.
DEFAULT_MARGIN = timedelta(minutes=45)

WINDOW_STATES = ("productive", "dark", "unknown")

WINDOW_LABELS = {
    "productive": "Período produtivo",
    "dark": "Fora do período produtivo",
    "unknown": "Período produtivo desconhecido",
}


@dataclass(frozen=True)
class ProductionWindow:
    """One installation's productive window for one day, in UTC."""

    state: str
    #: Apparent sunrise and sunset, before the margin. `None` when the state is
    #: `unknown`, and also on a polar day or night, where neither occurs.
    sunrise: datetime | None = None
    sunset: datetime | None = None
    #: The window the rules actually use: sunrise + margin to sunset - margin.
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    reason: str | None = None

    @property
    def label(self) -> str:
        return WINDOW_LABELS.get(self.state, self.state)

    @property
    def is_productive(self) -> bool:
        return self.state == "productive"

    @property
    def is_known(self) -> bool:
        return self.state != "unknown"


def _julian_day(moment: date) -> float:
    year, month = moment.year, moment.month
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + moment.day
        + b
        - 1524.5
    )


def _solar_geometry(julian_century: float) -> tuple[float, float]:
    """Solar declination in degrees and the equation of time in minutes."""
    geom_mean_long = (280.46646 + julian_century * (36000.76983 + julian_century * 0.0003032)) % 360
    geom_mean_anom = 357.52911 + julian_century * (35999.05029 - 0.0001537 * julian_century)
    eccentricity = 0.016708634 - julian_century * (0.000042037 + 0.0000001267 * julian_century)
    anom_rad = math.radians(geom_mean_anom)
    equation_of_centre = (
        math.sin(anom_rad) * (1.914602 - julian_century * (0.004817 + 0.000014 * julian_century))
        + math.sin(2 * anom_rad) * (0.019993 - 0.000101 * julian_century)
        + math.sin(3 * anom_rad) * 0.000289
    )
    true_long = geom_mean_long + equation_of_centre
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * julian_century))

    mean_obliquity = 23 + (26 + ((21.448 - julian_century * (46.815 + julian_century * (0.00059 - julian_century * 0.001813)))) / 60) / 60
    obliquity = mean_obliquity + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * julian_century))
    declination = math.degrees(
        math.asin(math.sin(math.radians(obliquity)) * math.sin(math.radians(apparent_long)))
    )

    var_y = math.tan(math.radians(obliquity / 2)) ** 2
    long_rad = math.radians(geom_mean_long)
    equation_of_time = 4 * math.degrees(
        var_y * math.sin(2 * long_rad)
        - 2 * eccentricity * math.sin(anom_rad)
        + 4 * eccentricity * var_y * math.sin(anom_rad) * math.cos(2 * long_rad)
        - 0.5 * var_y * var_y * math.sin(4 * long_rad)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * anom_rad)
    )
    return declination, equation_of_time


def _hour_angle_cosine(*, latitude: float, on: date, zenith_degrees: float) -> tuple[float, float]:
    """The cosine of the sunrise hour angle, and the equation of time.

    The cosine carries the polar cases in its sign, which is why it is returned
    rather than consumed on the spot: above +1 the sun never reaches the
    zenith and never rises; below -1 it never falls to the zenith and never
    sets. Re-deriving that distinction from the latitude afterwards is how the
    first version of this module got a polar day and a polar night the wrong
    way round.
    """
    julian_century = (_julian_day(on) + 0.5 - 2451545.0) / 36525.0
    declination, equation_of_time = _solar_geometry(julian_century)
    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    cosine = (
        math.cos(math.radians(zenith_degrees)) / (math.cos(lat_rad) * math.cos(dec_rad))
        - math.tan(lat_rad) * math.tan(dec_rad)
    )
    return cosine, equation_of_time


def sun_times(
    *, latitude: float, longitude: float, on: date, zenith_degrees: float = SUNRISE_ZENITH_DEGREES
) -> tuple[datetime | None, datetime | None]:
    """Apparent sunrise and sunset in UTC, or `(None, None)`.

    `None` means the sun neither rises nor sets on this day at this latitude --
    a polar day or a polar night. That is a real astronomical answer, not a
    failure, and the caller must not read it as midnight.
    """
    cos_hour_angle, equation_of_time = _hour_angle_cosine(
        latitude=latitude, on=on, zenith_degrees=zenith_degrees
    )
    if not -1.0 <= cos_hour_angle <= 1.0:
        return None, None
    hour_angle = math.degrees(math.acos(cos_hour_angle))

    solar_noon_minutes = 720 - 4 * longitude - equation_of_time
    sunrise_minutes = solar_noon_minutes - 4 * hour_angle
    sunset_minutes = solar_noon_minutes + 4 * hour_angle

    midnight = datetime.combine(on, time(0, 0), tzinfo=timezone.utc)
    return (
        midnight + timedelta(minutes=sunrise_minutes),
        midnight + timedelta(minutes=sunset_minutes),
    )


def window_for(
    *,
    latitude: Decimal | float | None,
    longitude: Decimal | float | None,
    at: datetime,
    margin: timedelta = DEFAULT_MARGIN,
) -> ProductionWindow:
    """Whether `at` falls inside this location's productive window.

    `at` must be timezone-aware. The whole calculation runs in UTC, so the
    installation's local timezone is never consulted and a daylight-saving
    change cannot move the sun.
    """
    if at.tzinfo is None:
        raise ValueError("production window needs an aware datetime")
    moment = at.astimezone(timezone.utc)

    if latitude is None or longitude is None:
        return ProductionWindow(
            "unknown",
            reason="a instalação não tem coordenadas registadas",
        )

    sunrise, sunset = sun_times(latitude=float(latitude), longitude=float(longitude), on=moment.date())
    if sunrise is None or sunset is None:
        # Polar day or polar night, told apart by the sign the hour-angle
        # cosine overflowed on: below -1 the sun never sets, above +1 it never
        # rises. No Solcor installation is anywhere near these latitudes; the
        # branch exists so the arithmetic cannot produce a midnight-shaped
        # answer if one ever is.
        cosine, _ = _hour_angle_cosine(
            latitude=float(latitude), on=moment.date(), zenith_degrees=SUNRISE_ZENITH_DEGREES
        )
        return ProductionWindow(
            "productive" if cosine < -1.0 else "dark",
            reason="o sol não nasce nem se põe nesta latitude neste dia",
        )

    starts_at = sunrise + margin
    ends_at = sunset - margin
    # A winter day shorter than twice the margin would otherwise invert the
    # window and read as dark at noon.
    if ends_at <= starts_at:
        midpoint = sunrise + (sunset - sunrise) / 2
        starts_at = ends_at = midpoint

    productive = starts_at <= moment <= ends_at
    return ProductionWindow(
        "productive" if productive else "dark",
        sunrise=sunrise,
        sunset=sunset,
        starts_at=starts_at,
        ends_at=ends_at,
        reason=None
        if productive
        else f"fora de {starts_at.strftime('%H:%M')}–{ends_at.strftime('%H:%M')} UTC",
    )
