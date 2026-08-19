"""getKpiStationDay answers with a row per day of the month, not per request.

Observed live on 2026-08-19: one request for a single station and a single day
returned 31 rows, every one carrying the same stationCode and differing only by
collectTime. Attributing the first row to the requested day silently files one
day's energy under another date, which is the worst kind of wrong for reporting.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from nemsei.integrations.fusionsolar.production import normalize_daily_production_row


STATION = "NE=157795675"


def month_row(day: int, value: float) -> dict:
    collected = datetime(2026, 7, day, tzinfo=timezone.utc)
    return {
        "collectTime": int(collected.timestamp() * 1000),
        "stationCode": STATION,
        "dataItemMap": {"PVYield": value, "inverterYield": value - 1},
    }


def test_a_row_carries_the_day_it_describes() -> None:
    sample = normalize_daily_production_row(month_row(24, 59.55))
    assert sample.external_id == STATION
    assert sample.value == Decimal("59.55")
    assert sample.source_timestamp == datetime(2026, 7, 24, tzinfo=timezone.utc)


def test_rows_of_one_month_are_distinguishable_only_by_timestamp() -> None:
    rows = [month_row(day, 50 + day) for day in range(1, 32)]
    samples = [normalize_daily_production_row(row) for row in rows]
    # Every row looks identical apart from its timestamp.
    assert len({sample.external_id for sample in samples}) == 1
    assert len({sample.source_timestamp for sample in samples}) == 31
    requested = date(2026, 7, 24)
    matching = [s for s in samples if s.source_timestamp.date() == requested]
    assert len(matching) == 1 and matching[0].value == Decimal("74")


def test_a_row_without_a_timestamp_cannot_be_attributed() -> None:
    row = month_row(24, 59.55)
    del row["collectTime"]
    assert normalize_daily_production_row(row).source_timestamp is None


def test_an_unusable_timestamp_is_refused_rather_than_guessed() -> None:
    row = month_row(24, 59.55)
    row["collectTime"] = "not-a-timestamp"
    with pytest.raises(ValueError, match="collectTime"):
        normalize_daily_production_row(row)
