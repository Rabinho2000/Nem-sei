"""Pin the parsing contract against a real captured FusionSolar response.

The row below is an unmodified `getKpiStationDay` payload that V1 stored for the
canary plant on 2026-07-24. It matters because the three candidate energy fields
disagree: V1 reads `PVYield`, `inverterYield` and `inverter_power` as a fallback
chain, while V2 accepts only `PVYield`. A synthetic fixture cannot demonstrate
that difference, because it is the provider that puts different numbers in those
fields.
"""
from __future__ import annotations

from decimal import Decimal

from nemsei.integrations.fusionsolar.production import normalize_daily_production_row


# Real provider row for plant NE=157795675 (11.4 kWp), day 2026-07-24.
CAPTURED_ROW = {
    "collectTime": 1784851200000,
    "stationCode": "NE=157795675",
    "dataItemMap": {
        "inverter_power": 59.55,
        "inverterYield": 58.9,
        "PVYield": 59.55,
        "perpower_ratio": 5.224,
        "installed_capacity": 11.4,
        "selfUsePower": 34.39,
        "selfProvide": 33.49,
        "use_power": 39.58,
        "chargeCap": 10.41,
        "dischargeCap": 9.51,
        "power_profit": 2.92,
        "reduction_total_co2": 0.028,
        "reduction_total_tree": 0.038,
    },
}


def test_real_payload_yields_the_contracted_pvyield_value() -> None:
    sample = normalize_daily_production_row(CAPTURED_ROW)
    assert sample.external_id == "NE=157795675"
    assert sample.value == Decimal("59.55")
    assert sample.quality == "complete" and sample.completeness == "complete"


def test_real_payload_fields_disagree_so_the_choice_is_not_incidental() -> None:
    values = CAPTURED_ROW["dataItemMap"]
    assert values["PVYield"] != values["inverterYield"]
    # V1's second fallback would have produced a different number for this day.
    assert Decimal(str(values["inverterYield"])) == Decimal("58.9")


def test_provider_arithmetic_confirms_the_kwh_unit() -> None:
    values = CAPTURED_ROW["dataItemMap"]
    ratio = Decimal(str(values["PVYield"])) / Decimal(str(values["installed_capacity"]))
    # The provider's own specific yield equals PVYield / installed kW, which only
    # holds if PVYield is kWh against a kW capacity.
    assert ratio.quantize(Decimal("0.001")) == Decimal(str(values["perpower_ratio"]))


def test_a_row_without_pvyield_is_missing_rather_than_zero() -> None:
    """The other two energy fields are still present, and must not be used."""
    row = dict(CAPTURED_ROW)
    row["dataItemMap"] = {key: value for key, value in CAPTURED_ROW["dataItemMap"].items() if key != "PVYield"}
    sample = normalize_daily_production_row(row)
    assert sample.value is None
    assert sample.quality == "missing" and sample.completeness == "partial"
