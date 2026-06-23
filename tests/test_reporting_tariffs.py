from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest

from monitoring_board.reporting.models import HourlyEnergyRecord, TariffConfig, TariffPeriodRule, TariffType
from monitoring_board.reporting.tariffs import (
    PERIOD_CHEIA,
    PERIOD_PONTA,
    PERIOD_SIMPLE,
    PERIOD_SUPER_VAZIO,
    PERIOD_VAZIO,
    TariffValidationError,
    classify_tariff_period,
    parse_tariff_type,
    value_tariff_energy,
)


def rule(day: str, start: str, end: str, period: str) -> TariffPeriodRule:
    return TariffPeriodRule(
        weekday_type=day,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        period_name=period,
    )


def config(tariff_type: TariffType, prices: dict[str, Decimal], rules: tuple[TariffPeriodRule, ...] = ()) -> TariffConfig:
    return TariffConfig(tariff_id=1, asset_id=1, tariff_type=tariff_type, prices=prices, rules=rules)


def hourly(moment: datetime, *, production: str = "0", self_use: str | None = None, export: str | None = None) -> HourlyEnergyRecord:
    return HourlyEnergyRecord(
        period_start=moment,
        period_end=moment + timedelta(hours=1),
        production_kwh=Decimal(production),
        self_use_kwh=Decimal(self_use) if self_use is not None else None,
        export_kwh=Decimal(export) if export is not None else None,
    )


def test_classification_handles_boundaries_overnight_weekday_weekend_and_precedence() -> None:
    rules = (
        rule("all", "00:00", "23:59", PERIOD_CHEIA),
        rule("weekday", "22:00", "07:00", PERIOD_VAZIO),
        rule("weekend", "00:00", "00:00", PERIOD_SUPER_VAZIO),
    )

    assert classify_tariff_period(datetime(2026, 1, 5, 21, 59), rules) == PERIOD_CHEIA
    assert classify_tariff_period(datetime(2026, 1, 5, 22, 0), rules) == PERIOD_VAZIO
    assert classify_tariff_period(datetime(2026, 1, 6, 6, 59), rules) == PERIOD_VAZIO
    assert classify_tariff_period(datetime(2026, 1, 10, 12, 0), rules) == PERIOD_SUPER_VAZIO


def test_validation_rejects_overlap_invalid_type_invalid_price_and_missing_required_price() -> None:
    with pytest.raises(TariffValidationError):
        value_tariff_energy(
            config(
                TariffType.TRI_HOURLY,
                {PERIOD_PONTA: Decimal("0.3"), PERIOD_CHEIA: Decimal("0.2")},
                (rule("all", "00:00", "12:00", PERIOD_PONTA),),
            )
        )

    with pytest.raises(TariffValidationError):
        parse_tariff_type("invalid")

    with pytest.raises(TariffValidationError):
        value_tariff_energy(
            config(
                TariffType.TRI_HOURLY,
                {PERIOD_PONTA: Decimal("0.3"), PERIOD_CHEIA: Decimal("0.2"), PERIOD_VAZIO: Decimal("0.1")},
                (rule("all", "00:00", "12:00", PERIOD_PONTA), rule("all", "11:00", "13:00", PERIOD_CHEIA)),
            )
        )


@pytest.mark.parametrize(
    ("tariff_type", "prices", "expected"),
    [
        (TariffType.SIMPLE, {PERIOD_SIMPLE: Decimal("0.20")}, Decimal("20.00")),
        (TariffType.BI_HOURLY, {PERIOD_CHEIA: Decimal("0.20"), PERIOD_VAZIO: Decimal("0.10")}, Decimal("6.00")),
        (TariffType.TRI_HOURLY, {PERIOD_PONTA: Decimal("0.30"), PERIOD_CHEIA: Decimal("0.20"), PERIOD_VAZIO: Decimal("0.10")}, Decimal("7.00")),
        (
            TariffType.TETRA_HOURLY,
            {PERIOD_PONTA: Decimal("0.30"), PERIOD_CHEIA: Decimal("0.20"), PERIOD_VAZIO: Decimal("0.10"), PERIOD_SUPER_VAZIO: Decimal("0.05")},
            Decimal("6.50"),
        ),
    ],
)
def test_valuation_supports_simple_bi_tri_and_tetra(tariff_type: TariffType, prices: dict[str, Decimal], expected: Decimal) -> None:
    if tariff_type == TariffType.SIMPLE:
        result = value_tariff_energy(config(tariff_type, prices), aggregate_self_use_kwh=Decimal("100"))
    else:
        rules = (
            rule("all", "08:00", "10:00", PERIOD_PONTA if PERIOD_PONTA in prices else PERIOD_CHEIA),
            rule("all", "10:00", "20:00", PERIOD_CHEIA),
            rule("all", "20:00", "23:00", PERIOD_VAZIO),
            rule("all", "23:00", "08:00", PERIOD_SUPER_VAZIO if PERIOD_SUPER_VAZIO in prices else PERIOD_VAZIO),
        )
        records = [
            hourly(datetime(2026, 1, 5, 8), production="20", self_use="10"),
            hourly(datetime(2026, 1, 5, 11), production="20", self_use="10"),
            hourly(datetime(2026, 1, 5, 21), production="20", self_use="10"),
            hourly(datetime(2026, 1, 5, 23), production="20", self_use="10"),
        ]
        result = value_tariff_energy(config(tariff_type, prices, rules), hourly_records=records)

    assert result.total_value_eur == expected


def test_energy_rules_use_self_consumption_and_keep_export_separate() -> None:
    rules = (rule("all", "00:00", "00:00", PERIOD_CHEIA),)
    tariff = config(TariffType.BI_HOURLY, {PERIOD_CHEIA: Decimal("0.20"), PERIOD_VAZIO: Decimal("0.10")}, rules)

    explicit = value_tariff_energy(tariff, hourly_records=[hourly(datetime(2026, 1, 5, 10), production="10", self_use="4", export="6")])
    inferred = value_tariff_energy(tariff, hourly_records=[hourly(datetime(2026, 1, 5, 11), production="10", export="6")])
    missing = value_tariff_energy(tariff, hourly_records=[hourly(datetime(2026, 1, 5, 12), production="10")])
    clamped = value_tariff_energy(tariff, hourly_records=[hourly(datetime(2026, 1, 5, 13), production="4", export="9")])

    assert explicit.total_value_eur == Decimal("0.80")
    assert inferred.total_value_eur == Decimal("0.80")
    assert "inferred_hourly_self_use" in inferred.warnings
    assert missing.total_value_eur is None
    assert "missing_hourly_self_use" in missing.warnings
    assert clamped.total_energy_kwh == Decimal("0")
