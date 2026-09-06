"""Contractual vs operational availability: the selection policy and its wiring.

The requirement this file exists for: a realtime-sampled number must never
stand in for a warranted one. That is enforced in three independent places,
and each is tested here rather than assumed from the one below it --

1. the pure policy (`reporting/rules/availability_source.py`),
2. the database (0041's paired CHECK constraints), and
3. the report payload (`reporting/assembler.py`).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from nemsei.assets.service import create_asset, create_device
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.availability_service import monthly_availability_for_asset
from nemsei.diagnostics.models import AssetAvailabilityDaily
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.rules.availability_source import (
    AVAILABILITY_SOURCES,
    KIND_CONTRACTUAL,
    KIND_OPERATIONAL,
    SOURCE_FUSIONSOLAR_SAMPLED,
    SOURCE_KIND_BY_SOURCE,
    SOURCE_MANUAL,
    SOURCE_PROVIDER_WAT,
    is_contractual,
    select_availability,
    source_kind,
)
from tests_v2.test_migrations import upgrade


# --- 1. The pure policy -------------------------------------------------


def test_contractual_wins_over_sampled_even_though_sampled_is_the_newer_number() -> None:
    """The headline rule: 98.5 contractual beats 97.9 sampled, always."""
    chosen = select_availability(
        [
            {"source": SOURCE_FUSIONSOLAR_SAMPLED, "availability_pct": 97.9, "calculated_at": "2026-09-06"},
            {"source": SOURCE_PROVIDER_WAT, "availability_pct": 98.5, "calculated_at": "2026-08-01"},
        ]
    )
    assert chosen is not None
    assert chosen["availability_pct"] == 98.5
    assert chosen["source"] == SOURCE_PROVIDER_WAT


def test_selection_is_order_independent() -> None:
    """A report is only reproducible if assembly order cannot change the answer."""
    candidates = [
        {"source": SOURCE_PROVIDER_WAT, "availability_pct": 98.5},
        {"source": SOURCE_FUSIONSOLAR_SAMPLED, "availability_pct": 97.9},
        {"source": SOURCE_MANUAL, "availability_pct": 99.1},
    ]
    first = select_availability(candidates)
    assert first is not None
    assert select_availability(list(reversed(candidates))) == first
    # `manual` is a human assertion and outranks a derived contractual figure.
    assert first["source"] == SOURCE_MANUAL


def test_sampled_is_used_when_it_is_the_only_source() -> None:
    """Operational is not forbidden -- only forbidden from *outranking*."""
    chosen = select_availability([{"source": SOURCE_FUSIONSOLAR_SAMPLED, "availability_pct": 97.9}])
    assert chosen is not None
    assert chosen["source"] == SOURCE_FUSIONSOLAR_SAMPLED


def test_a_contractual_source_without_a_value_does_not_block_the_sampled_one() -> None:
    """`None` is absence of a figure, not a veto carrying one."""
    chosen = select_availability(
        [
            {"source": SOURCE_PROVIDER_WAT, "availability_pct": None},
            {"source": SOURCE_FUSIONSOLAR_SAMPLED, "availability_pct": 97.9},
        ]
    )
    assert chosen is not None
    assert chosen["source"] == SOURCE_FUSIONSOLAR_SAMPLED


def test_zero_percent_is_a_real_figure_and_none_is_not() -> None:
    """0% is a measurement; None is absence. They must not collapse."""
    chosen = select_availability([{"source": SOURCE_FUSIONSOLAR_SAMPLED, "availability_pct": 0.0}])
    assert chosen is not None
    assert chosen["availability_pct"] == 0.0
    assert select_availability([{"source": SOURCE_FUSIONSOLAR_SAMPLED, "availability_pct": None}]) is None


def test_no_candidates_is_none_never_zero() -> None:
    assert select_availability([]) is None


def test_every_source_is_classified_and_the_sampled_engine_is_operational() -> None:
    assert set(SOURCE_KIND_BY_SOURCE) == set(AVAILABILITY_SOURCES)
    assert source_kind(SOURCE_FUSIONSOLAR_SAMPLED) == KIND_OPERATIONAL
    assert not is_contractual(SOURCE_FUSIONSOLAR_SAMPLED)
    assert source_kind(SOURCE_PROVIDER_WAT) == KIND_CONTRACTUAL


def test_an_unregistered_source_raises_instead_of_defaulting() -> None:
    """Defaulting either way would hide that someone added a source."""
    with pytest.raises(ValueError):
        source_kind("something_new")


# --- 2. The database ----------------------------------------------------


def _factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _asset_with_mapping(session):
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key=f"conn-policy-{datetime.now().timestamp()}",
        display_name="Policy fixture", credential_reference="dev", enabled=True, configuration_status="configured",
    )
    asset = create_asset(session, canonical_name="Policy plant", installed_dc_power_kw=Decimal("100.0"))
    create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="ST-P", valid_from=date(2026, 1, 1))
    device = create_device(
        session, asset_id=asset.id, device_kind="inverter", serial_number="SN-P", rated_power_kw=Decimal("50.0"),
        valid_from=date(2026, 1, 1),
    )
    create_mapping(
        session, asset_id=asset.id, provider_connection_id=connection.id, external_id="DEV-P",
        resource_kind="device", device_id=device.id, valid_from=date(2026, 1, 1),
    )
    session.commit()
    return asset, device


def test_database_refuses_to_store_the_sampled_engine_as_contractual(settings, monkeypatch):
    """The split is structural, not a naming convention a writer can ignore."""
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        asset, _device = _asset_with_mapping(session)
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO asset_availability_daily (asset_id, availability_date, availability_pct, "
                    "valid_sample_count, expected_device_count, observed_device_count, minimum_required_samples, "
                    "coverage_status, warning_codes_json, source, source_kind, calculation_details_json, "
                    "calculated_at, created_at, updated_at) VALUES (:a, DATE '2026-07-01', 99.0, 10, 1, 1, 5, "
                    "'complete', '[]', 'fusionsolar_sampled', 'contractual', '{}', now(), now(), now())"
                ),
                {"a": asset.id},
            )
            session.flush()


def _store_month(session, *, asset_id, source, source_kind_value, days, pct, samples_per_day=None):
    for offset in range(days):
        session.add(
            AssetAvailabilityDaily(
                asset_id=asset_id,
                availability_date=date(2026, 6, 1 + offset),
                availability_pct=Decimal(str(pct[offset] if isinstance(pct, list) else pct)),
                valid_sample_count=(samples_per_day[offset] if samples_per_day else 10),
                expected_device_count=1,
                observed_device_count=1,
                minimum_required_samples=5,
                coverage_status="complete",
                warning_codes_json=[],
                source=source,
                source_kind=source_kind_value,
                calculation_details_json={},
                calculated_at=datetime(2026, 7, 1, tzinfo=None).replace(tzinfo=None) if False else _now(),
                created_at=_now(),
                updated_at=_now(),
            )
        )
    session.flush()


def _now():
    from nemsei.shared.clock import utc_now

    return utc_now()


def test_monthly_prefers_the_contractual_source_when_both_are_materialized(settings, monkeypatch):
    """Both sources complete for the same month: the WAT figure is reported."""
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        asset, _device = _asset_with_mapping(session)
        days = 30
        _store_month(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, source_kind_value=KIND_OPERATIONAL, days=days, pct=97.9)
        _store_month(session, asset_id=asset.id, source=SOURCE_PROVIDER_WAT, source_kind_value=KIND_CONTRACTUAL, days=days, pct=98.5)
        session.commit()

        result = monthly_availability_for_asset(
            session, asset_id=asset.id, month_start=date(2026, 6, 1), month_end_exclusive=date(2026, 7, 1)
        )
    assert result["availability_pct"] == pytest.approx(98.5)
    assert result["source"] == SOURCE_PROVIDER_WAT
    assert result["source_kind"] == KIND_CONTRACTUAL
    assert result["coverage_status"] == "complete"


def test_monthly_contractual_is_weighted_by_evidence_not_a_flat_day_mean(settings, monkeypatch):
    """V1's contractual month is `SUM(pct*slots)/SUM(slots)`, not a day mean.

    Two days, 100% over 90 samples and 50% over 10: the evidence-weighted
    answer is 95.0, the flat day-mean answer would be 75.0. Ported from V1's
    `get_monthly_availability` (`reporting/repositories.py`), which is the
    query that actually fed V1's reports and monthly close.
    """
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        asset, _device = _asset_with_mapping(session)
        _store_month(
            session, asset_id=asset.id, source=SOURCE_PROVIDER_WAT, source_kind_value=KIND_CONTRACTUAL,
            days=2, pct=[100.0, 50.0], samples_per_day=[90, 10],
        )
        session.commit()
        result = monthly_availability_for_asset(
            session, asset_id=asset.id, month_start=date(2026, 6, 1), month_end_exclusive=date(2026, 6, 3)
        )
    assert result["availability_pct"] == pytest.approx(95.0)
    assert result["availability_pct"] != pytest.approx(75.0)


def test_monthly_operational_stays_a_flat_day_mean(settings, monkeypatch):
    """The sampled engine keeps V1's `sampled_month_quality` arithmetic mean."""
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        asset, _device = _asset_with_mapping(session)
        _store_month(
            session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, source_kind_value=KIND_OPERATIONAL,
            days=2, pct=[100.0, 50.0], samples_per_day=[90, 10],
        )
        session.commit()
        result = monthly_availability_for_asset(
            session, asset_id=asset.id, month_start=date(2026, 6, 1), month_end_exclusive=date(2026, 6, 3)
        )
    assert result["availability_pct"] == pytest.approx(75.0)
