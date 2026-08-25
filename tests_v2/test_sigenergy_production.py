"""Sigenergy daily history: the rules carried from V1, and the ones V2 adds.

V1's parser is the evidence base -- it is the only working implementation of
this contract anywhere -- so the rules it enforces are pinned here rather than
re-derived. The two rules V2 adds on top are pinned too, because they are the
reason this could not simply be copied.
"""
from __future__ import annotations

import pytest

from nemsei.integrations.sigenergy.production import (
    CORE_METRICS,
    SigenergyHistoryUnitError,
    parse_daily_history,
)


def payload(**overrides):
    base = {
        "unit": "kWh",
        "powerGenerationKwh": 120.5,
        "powerUseKwh": 80.0,
        "powerOneselfKwh": 60.0,
        "powerToGridKwh": 60.5,
        "powerFromGridKwh": 20.0,
    }
    base.update(overrides)
    return base


def test_the_five_core_metrics_map_to_v2_vocabulary() -> None:
    parsed = parse_daily_history(payload(), confirmed_unit="")

    assert set(parsed.values) == set(CORE_METRICS)
    assert parsed.values["production_energy"] == 120.5
    assert parsed.values["export_energy"] == 60.5
    assert parsed.quality == "complete"


def test_the_newer_field_wins_over_the_legacy_one() -> None:
    # V1 learned this the hard way: both names can be present in one payload.
    parsed = parse_daily_history(payload(powerGenerationKwh=100.0, powerGeneration=999.0), confirmed_unit="")

    assert parsed.values["production_energy"] == 100.0


def test_the_legacy_field_is_used_when_the_newer_one_is_absent() -> None:
    body = payload()
    del body["powerGenerationKwh"]
    body["powerGeneration"] = 77.0

    assert parse_daily_history(body, confirmed_unit="").values["production_energy"] == 77.0


def test_an_unconfirmed_unit_is_refused_rather_than_assumed() -> None:
    # The history payload does not carry a unit in every response. A value
    # whose unit nobody verified is not a value.
    body = payload()
    del body["unit"]

    with pytest.raises(SigenergyHistoryUnitError):
        parse_daily_history(body, confirmed_unit="")


def test_an_operator_confirmation_stands_in_for_a_missing_payload_unit() -> None:
    body = payload()
    del body["unit"]

    assert parse_daily_history(body, confirmed_unit="kWh").values["production_energy"] == 120.5


def test_a_payload_unit_that_is_not_kwh_is_refused_even_if_an_operator_confirmed_kwh() -> None:
    # The payload's own statement wins: if it says Wh, an operator's blanket
    # kWh confirmation does not make the number kWh.
    with pytest.raises(SigenergyHistoryUnitError):
        parse_daily_history(payload(unit="Wh"), confirmed_unit="kWh")


def test_a_missing_metric_stays_missing_and_the_day_reads_partial() -> None:
    body = payload()
    del body["powerFromGridKwh"]

    parsed = parse_daily_history(body, confirmed_unit="")

    assert parsed.values["grid_import_energy"] is None
    assert parsed.quality == "partial"
    assert parsed.completeness == "partial"


def test_a_day_with_no_readings_at_all_is_missing_not_zero() -> None:
    parsed = parse_daily_history({"unit": "kWh"}, confirmed_unit="")

    assert all(value is None for value in parsed.values.values())
    assert parsed.quality == "missing"


def test_a_negative_or_non_numeric_reading_is_rejected(caplog) -> None:
    for bad in (-1.0, "abc", True):
        with pytest.raises(ValueError):
            parse_daily_history(payload(powerGenerationKwh=bad), confirmed_unit="")


def test_battery_counters_are_kept_as_evidence_not_as_metrics() -> None:
    # production_facts.metric_kind has no vocabulary for charge/discharge, and
    # inventing one to hold a number nothing reads would be schema debt.
    parsed = parse_daily_history(payload(esChargingKwh=5.0, esDischarging=3.0), confirmed_unit="")

    assert parsed.battery == {"battery_charge_kwh": 5.0, "battery_discharge_kwh": 3.0}
    assert "battery_charge_kwh" not in parsed.values


def test_self_use_reads_load_green_power_not_the_generation_side_counter() -> None:
    # V1's own comment: powerOneself is the value that balances the reports;
    # powerSelfConsumption is a different number and must not be used here.
    parsed = parse_daily_history(payload(powerOneselfKwh=42.0, powerSelfConsumption=999.0), confirmed_unit="")

    assert parsed.values["self_use_energy"] == 42.0


class StubClient:
    """A Sigenergy client that answers from a canned payload.

    The wire contract was verified against the live API (three days matching
    V1's stored values exactly); this proves the other half -- that an accepted
    payload becomes canonical facts -- without spending provider calls on an
    account that throttles.
    """

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requested: list = []

    def authenticate(self) -> None:
        return None

    def get_system_history(self, system_id: str, *, target_date, level: str = "Day") -> dict:
        self.requested.append((system_id, target_date, level))
        return self.payload


def test_an_accepted_day_becomes_five_canonical_facts(settings, monkeypatch) -> None:
    from datetime import date

    from sqlalchemy import select

    from nemsei.assets.service import create_asset
    from nemsei.db import build_engine, build_session_factory
    from nemsei.integrations.sigenergy.production import SigenergyProductionService
    from nemsei.monitoring.models import ProductionFact
    from nemsei.providers.service import create_connection, create_mapping
    from nemsei.sources.service import create_source_policy
    from tests_v2.test_migrations import upgrade

    upgrade(settings, monkeypatch)
    for name, value in (
        ("NEMSEI_V2_SIGENERGY_REF_APP_KEY", "k"), ("NEMSEI_V2_SIGENERGY_REF_APP_SECRET", "s"),
        ("NEMSEI_V2_SIGENERGY_REF_BASE_URL", "https://example.invalid"),
        ("NEMSEI_V2_SIGENERGY_REF_AUTH_ENDPOINT", "/a"), ("NEMSEI_V2_SIGENERGY_REF_SYSTEMS_ENDPOINT", "/s"),
        ("NEMSEI_V2_SIGENERGY_REF_ENERGY_FLOW_ENDPOINT", "/e"), ("NEMSEI_V2_SIGENERGY_REF_REGION", "eu"),
        ("NEMSEI_V2_SIGENERGY_REF_PRODUCTION_TIMEZONE", "Europe/Lisbon"),
        ("NEMSEI_V2_SIGENERGY_REF_PRODUCTION_UNIT", "kWh"),
        ("NEMSEI_V2_PROVIDER_READS", "true"),
    ):
        monkeypatch.setenv(name, value)

    factory = build_session_factory(build_engine(settings))
    session = factory()
    asset = create_asset(session, canonical_name="Sigen Plant", timezone="Europe/Lisbon")
    connection = create_connection(
        session, provider_code="sigenergy", connection_key="live", display_name="Sigen",
        credential_reference="ref", enabled=True, configuration_status="configured",
    )
    session.flush()
    mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id,
                             external_id="SYS1", valid_from=date(2026, 8, 1))
    create_source_policy(session, asset_id=asset.id, provider_mapping_id=mapping.id,
                         source_use="production", priority=1, valid_from=date(2026, 8, 1))
    session.commit()
    asset_id, connection_id = asset.id, connection.id
    session.close()

    stub = StubClient(payload(esChargingKwh=7.5))
    service = SigenergyProductionService(
        factory, settings.__class__.from_environment(),
        client_factory=lambda credentials, endpoints, transport: stub,
    )
    result = service.sync_daily_production(connection_id, start_date=date(2026, 8, 18), end_date=date(2026, 8, 18))

    assert result.status == "success"
    assert result.days_accepted == 1
    session = factory()
    facts = list(session.scalars(select(ProductionFact).where(ProductionFact.asset_id == asset_id)))
    by_metric = {fact.metric_kind: fact for fact in facts}
    assert set(by_metric) == set(CORE_METRICS)
    assert float(by_metric["production_energy"].value) == 120.5
    assert by_metric["production_energy"].unit == "kWh"
    # The day is anchored in the verified source timezone, not the server's.
    assert by_metric["production_energy"].metadata_json["source_timezone"] == "Europe/Lisbon"
    # Battery counters survive as evidence without becoming a metric.
    assert by_metric["production_energy"].metadata_json["battery_charge_kwh"] == 7.5
    session.close()
    assert stub.requested == [("SYS1", date(2026, 8, 18), "Day")]
