"""Power samples into daily energy, and everything that must refuse to happen.

The integration itself is arithmetic and is tested as arithmetic. The rest of
this file is about restraint: no export without a verified sign convention, no
self-consumption when a battery is moving energy, no facts for a mapping the
asset's own source policy did not select, and no second copy of a day that was
integrated twice.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.huawei_scada.models import HuaweiScadaPowerSample
from nemsei.integrations.huawei_scada.retention import purge_samples
from nemsei.integrations.huawei_scada.rollup import (
    HuaweiScadaRollupService,
    battery_moved_energy,
    current_samples,
    integrate,
)
from nemsei.integrations.huawei_scada.service import HuaweiScadaConfigurationError, contract_for
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.models import ProviderConnection
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import SyncRun
from tests_v2.test_migrations import upgrade

LISBON = ZoneInfo("Europe/Lisbon")
DAY = date(2026, 8, 24)
SERIAL = "HV2340123456"


def configured(monkeypatch, **overrides) -> None:
    monkeypatch.setenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_POWER_UNIT", "kW")
    monkeypatch.setenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_PRODUCTION_SIGNAL", "total_active_power")
    for name, value in overrides.items():
        monkeypatch.setenv(f"NEMSEI_V2_HUAWEI_SCADA_PRIMARY_{name}", value)


@pytest.fixture
def world(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    with factory() as session, session.begin():
        connection = create_connection(
            session,
            provider_code="huawei_scada",
            connection_key="scada-pilot",
            display_name="Huawei SCADA pilot",
            credential_reference="primary",
            enabled=True,
            configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Piloto SDongle", timezone="Europe/Lisbon")
        session.flush()
        mapping = create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id=SERIAL,
            valid_from=date(2026, 1, 1),
        )
        session.flush()
        create_source_policy(
            session,
            asset_id=asset.id,
            provider_mapping_id=mapping.id,
            source_use="production",
            priority=1,
            valid_from=date(2026, 1, 1),
        )
        ids = {"connection": connection.id, "asset": asset.id, "mapping": mapping.id}
    return factory, ids


def add_series(
    factory,
    ids,
    *,
    start_local: datetime,
    count: int,
    cadence: timedelta = timedelta(minutes=5),
    total_kw: Decimal | None = Decimal("10"),
    load_kw: Decimal | None = Decimal("4"),
    grid_kw: Decimal | None = Decimal("-6"),
    battery_kw: Decimal | None = Decimal("0"),
    session_id: int | None = None,
) -> None:
    """A constant-power series, so the expected integral is arithmetic by hand."""
    with factory() as session, session.begin():
        for index in range(count):
            moment = (start_local + cadence * index).astimezone(datetime_timezone.utc)
            session.add(
                HuaweiScadaPowerSample(
                    asset_id=ids["asset"],
                    provider_mapping_id=ids["mapping"],
                    session_id=session_id,
                    dongle_serial=SERIAL,
                    source_sample_key=f"huawei-scada:{SERIAL}:{moment.isoformat()}",
                    source_revision=1,
                    observed_at=moment,
                    ingested_at=moment,
                    pv_input_power_kw=total_kw,
                    load_power_kw=load_kw,
                    grid_power_kw=grid_kw,
                    battery_power_kw=battery_kw,
                    total_active_power_kw=total_kw,
                    raw_registers_json={},
                    quality="complete",
                    completeness="complete",
                    session_state="polling",
                    metadata_json={},
                )
            )


def full_day(factory, ids, **kwargs) -> None:
    """00:00 to 23:55 local, 5-minute cadence: a day covered edge to edge."""
    add_series(factory, ids, start_local=datetime(2026, 8, 24, 0, 0, tzinfo=LISBON), count=288, **kwargs)


def facts(factory, *, metric: str | None = None) -> list[ProductionFact]:
    with factory() as session:
        statement = select(ProductionFact).order_by(ProductionFact.id)
        if metric:
            statement = statement.where(ProductionFact.metric_kind == metric)
        return list(session.scalars(statement))


def roll(factory, settings, ids, **kwargs):
    return HuaweiScadaRollupService(factory, settings).roll_up(
        ids["connection"], lookback_days=kwargs.pop("lookback_days", 1), today=kwargs.pop("today", DAY)
    )


# --- the arithmetic ----------------------------------------------------------


class Sample:
    """A stand-in with only what `integrate` reads, so the maths is testable alone."""

    def __init__(self, offset_seconds, value, *, session_id=1, column="total_active_power_kw"):
        base = datetime(2026, 8, 24, 0, 0, tzinfo=datetime_timezone.utc)
        self.observed_at = base + timedelta(seconds=offset_seconds)
        self.session_id = session_id
        setattr(self, column, None if value is None else Decimal(str(value)))
        for other in ("total_active_power_kw", "load_power_kw", "grid_power_kw", "battery_power_kw"):
            if not hasattr(self, other):
                setattr(self, other, None)


def test_constant_power_for_an_hour_integrates_to_that_many_kilowatt_hours() -> None:
    samples = [Sample(second, 10) for second in range(0, 3601, 300)]
    result = integrate(samples, column="total_active_power_kw", max_gap_seconds=900)
    assert result.energy_kwh == Decimal("10")
    assert result.covered_seconds == 3600
    assert result.gap_count == 0


def test_a_ramp_integrates_by_the_trapezoid_rule() -> None:
    # 0 kW to 10 kW over one hour: the area under the line is 5 kWh.
    samples = [Sample(0, 0), Sample(3600, 10)]
    assert integrate(samples, column="total_active_power_kw", max_gap_seconds=3600).energy_kwh == Decimal("5")


def test_a_gap_wider_than_the_cap_is_excluded_never_bridged() -> None:
    """Bridging a four-hour outage would invent the energy in the middle."""
    samples = [Sample(0, 10), Sample(300, 10), Sample(14700, 10), Sample(15000, 10)]
    result = integrate(samples, column="total_active_power_kw", max_gap_seconds=900)
    # Two covered segments of 300 s each, and nothing claimed for the gap.
    assert result.energy_kwh == Decimal("10") * Decimal(600) / Decimal(3600)
    assert result.gap_count == 1
    assert result.largest_gap_seconds == 14400
    assert result.covered_seconds == 600


def test_an_absent_register_breaks_the_series_rather_than_reading_as_zero() -> None:
    samples = [Sample(0, 10), Sample(300, None), Sample(600, 10)]
    result = integrate(samples, column="total_active_power_kw", max_gap_seconds=900)
    assert result.energy_kwh == Decimal("0")
    assert result.covered_seconds == 0
    assert result.sample_count == 2


def test_time_running_backwards_is_counted_as_an_anomaly_and_adds_no_energy() -> None:
    samples = [Sample(600, 10), Sample(0, 10), Sample(900, 10)]
    result = integrate(samples, column="total_active_power_kw", max_gap_seconds=900)
    assert result.clock_anomalies == 1
    assert result.covered_seconds == 900


def test_a_reconnect_inside_the_gap_cap_is_recorded_but_still_integrated() -> None:
    samples = [Sample(0, 10, session_id=1), Sample(300, 10, session_id=2)]
    result = integrate(samples, column="total_active_power_kw", max_gap_seconds=900)
    assert result.session_boundaries == 1
    assert result.energy_kwh > 0


def test_clipping_splits_a_signed_series_into_its_two_directions() -> None:
    samples = [Sample(0, -6, column="grid_power_kw"), Sample(3600, -6, column="grid_power_kw")]
    negative = integrate(samples, column="grid_power_kw", max_gap_seconds=3600, clip="negative")
    positive = integrate(samples, column="grid_power_kw", max_gap_seconds=3600, clip="positive")
    assert negative.energy_kwh == Decimal("6")
    assert positive.energy_kwh == Decimal("0")


def test_a_series_with_no_usable_point_has_no_energy_rather_than_zero_energy() -> None:
    result = integrate([Sample(0, None)], column="total_active_power_kw", max_gap_seconds=900)
    assert result.energy_kwh is None and not result.has_energy


# --- the contract refuses rather than guesses ---------------------------------


def test_the_rollup_refuses_to_run_without_a_verified_production_signal(world, settings, monkeypatch) -> None:
    """37498 and 37516 are different measurements; there is no default."""
    factory, ids = world
    monkeypatch.setenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_POWER_UNIT", "kW")
    monkeypatch.delenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_PRODUCTION_SIGNAL", raising=False)
    full_day(factory, ids)

    with pytest.raises(HuaweiScadaConfigurationError, match="production signal"):
        roll(factory, settings, ids)
    assert facts(factory) == []


def test_the_rollup_refuses_a_power_unit_nobody_verified(world, settings, monkeypatch) -> None:
    factory, ids = world
    monkeypatch.setenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_POWER_UNIT", "")
    monkeypatch.setenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_PRODUCTION_SIGNAL", "total_active_power")

    with pytest.raises(HuaweiScadaConfigurationError, match="power unit"):
        roll(factory, settings, ids)


def test_self_use_derivation_requires_the_grid_sign_convention_it_depends_on(world, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch, SELF_USE_DERIVATION="consumption_minus_grid_import")
    with factory() as session:
        connection = session.get(ProviderConnection, ids["connection"])
        with pytest.raises(HuaweiScadaConfigurationError, match="grid sign convention"):
            contract_for(connection)


# --- what the rollup writes ---------------------------------------------------


def test_a_full_day_becomes_production_and_consumption_energy(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)

    result = roll(factory, settings, ids)

    assert result.facts_written == 2
    production = facts(factory, metric="production_energy")[0]
    # 10 kW held across 23 h 55 min of covered time.
    assert production.value == Decimal("239.166667")
    assert production.unit == "kWh"
    assert production.granularity == "day"
    assert facts(factory, metric="consumption_energy")[0].value == Decimal("95.666667")


def test_every_fact_declares_that_it_is_an_estimate_and_how_it_was_made(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)

    roll(factory, settings, ids)

    production = facts(factory, metric="production_energy")[0]
    assert production.quality == "partial"
    assert production.metadata_json["measurement_method"] == "power_integral"
    assert production.metadata_json["estimated"] is True
    assert production.metadata_json["integration_rule"] == "trapezoidal"
    assert production.metadata_json["source_signal"] == "total_active_power"
    assert production.metadata_json["asset_timezone"] == "Europe/Lisbon"
    assert production.metadata_json["sample_count"] == 288


def test_the_day_is_the_assets_local_day_not_a_utc_one(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)

    roll(factory, settings, ids)

    production = facts(factory, metric="production_energy")[0]
    # Local midnight in Lisbon during summer is 23:00 UTC the day before.
    assert production.period_start == datetime(2026, 8, 23, 23, 0, tzinfo=datetime_timezone.utc)
    assert production.period_end == datetime(2026, 8, 24, 23, 0, tzinfo=datetime_timezone.utc)


def test_a_day_covered_edge_to_edge_is_complete_and_a_partial_window_is_not(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)
    roll(factory, settings, ids)
    assert facts(factory, metric="production_energy")[0].completeness == "complete"


def test_a_day_sampled_only_in_daylight_is_partial_however_dense_it_was(world, settings, monkeypatch) -> None:
    """Coverage of the hours you saw says nothing about the ones you did not."""
    factory, ids = world
    configured(monkeypatch)
    add_series(factory, ids, start_local=datetime(2026, 8, 24, 9, 0, tzinfo=LISBON), count=96)

    roll(factory, settings, ids)

    production = facts(factory, metric="production_energy")[0]
    assert production.completeness == "partial"
    assert production.metadata_json["coverage_ratio"] < 0.4


def test_no_samples_means_no_fact_rather_than_a_zero(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)

    result = roll(factory, settings, ids)

    assert facts(factory) == []
    assert result.skipped_reasons.get("no_samples") == 1


def test_re_running_the_rollup_supersedes_the_day_rather_than_duplicating_it(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    add_series(factory, ids, start_local=datetime(2026, 8, 24, 9, 0, tzinfo=LISBON), count=12)
    roll(factory, settings, ids)
    first = facts(factory, metric="production_energy")[0].value

    # A late-arriving stretch of the same day, then a second pass.
    add_series(factory, ids, start_local=datetime(2026, 8, 24, 10, 0, tzinfo=LISBON), count=12)
    roll(factory, settings, ids)

    rows = facts(factory, metric="production_energy")
    assert [row.source_revision for row in rows] == [1, 2]
    assert rows[1].supersedes_fact_id == rows[0].id
    assert rows[1].value > first
    assert len({row.source_fact_key for row in rows}) == 1


def test_the_rollup_makes_no_provider_call_and_opens_no_sync_run(world, settings, monkeypatch) -> None:
    """It is a job over rows already held, not a synchronisation."""
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)

    roll(factory, settings, ids)

    with factory() as session:
        assert session.scalar(select(SyncRun)) is None
    assert all(fact.sync_run_id is None for fact in facts(factory))


# --- grid flows and self-consumption -----------------------------------------


def test_without_a_verified_sign_convention_no_grid_flows_are_written(world, settings, monkeypatch) -> None:
    """Half-right is not an option: 37502 is signed and undocumented."""
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)

    result = roll(factory, settings, ids)

    assert facts(factory, metric="export_energy") == []
    assert facts(factory, metric="grid_import_energy") == []
    assert result.skipped_reasons.get("grid_sign_convention_unverified") == 1


def test_a_verified_convention_splits_grid_power_into_export_and_import(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch, GRID_SIGN_CONVENTION="positive_import")
    full_day(factory, ids, grid_kw=Decimal("-6"))

    roll(factory, settings, ids)

    # Negative means export under this convention, so all of it is export.
    assert facts(factory, metric="export_energy")[0].value == Decimal("143.500000")
    assert facts(factory, metric="grid_import_energy")[0].value == Decimal("0.000000")
    assert facts(factory, metric="export_energy")[0].metadata_json["grid_sign_convention"] == "positive_import"


def test_the_opposite_convention_reverses_the_two(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch, GRID_SIGN_CONVENTION="positive_export")
    full_day(factory, ids, grid_kw=Decimal("-6"))

    roll(factory, settings, ids)

    assert facts(factory, metric="export_energy")[0].value == Decimal("0.000000")
    assert facts(factory, metric="grid_import_energy")[0].value == Decimal("143.500000")


def test_self_use_is_derived_only_when_asked_for_and_only_without_battery_flow(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(
        monkeypatch,
        GRID_SIGN_CONVENTION="positive_import",
        SELF_USE_DERIVATION="consumption_minus_grid_import",
    )
    full_day(factory, ids, grid_kw=Decimal("-6"), battery_kw=Decimal("0"))

    roll(factory, settings, ids)

    self_use = facts(factory, metric="self_use_energy")[0]
    # Consumption 95.666667, grid import 0 under this convention.
    assert self_use.value == Decimal("95.666667")
    assert self_use.metadata_json["derivation"] == "consumption_minus_grid_import"
    assert self_use.metadata_json["estimated"] is True


def test_a_day_with_battery_flow_gets_no_derived_self_use(world, settings, monkeypatch) -> None:
    """The battery absorbs the difference, so the identity does not hold.

    This is the same reason `monitoring/models.py` refuses to derive between
    energy metrics in general.
    """
    factory, ids = world
    configured(
        monkeypatch,
        GRID_SIGN_CONVENTION="positive_import",
        SELF_USE_DERIVATION="consumption_minus_grid_import",
    )
    full_day(factory, ids, battery_kw=Decimal("-2.5"))

    result = roll(factory, settings, ids)

    assert facts(factory, metric="self_use_energy") == []
    assert result.skipped_reasons.get("self_use_not_derivable_with_battery_flow") == 1
    assert any("battery_flow" in warning for warning in result.warnings)


def test_battery_flow_detection_is_any_flow_at_all_not_a_threshold() -> None:
    quiet = [Sample(0, 0, column="battery_power_kw")]
    trickle = [Sample(0, Decimal("0.2"), column="battery_power_kw")]
    assert not battery_moved_energy(quiet)
    assert battery_moved_energy(trickle)


# --- source policy ------------------------------------------------------------


def test_a_mapping_that_is_not_the_selected_production_source_writes_nothing(
    world, settings, monkeypatch
) -> None:
    """Otherwise the dataset, which totals every mapping, would double-count."""
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)
    with factory() as session, session.begin():
        # A second, higher-priority production source for the same asset.
        other = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="fusion",
            display_name="Fusion",
            credential_reference="dev",
            enabled=True,
            configuration_status="configured",
        )
        session.flush()
        rival = create_mapping(
            session, asset_id=ids["asset"], provider_connection_id=other.id, external_id="NE=1", valid_from=date(2026, 1, 1)
        )
        session.flush()
        create_source_policy(
            session,
            asset_id=ids["asset"],
            provider_mapping_id=rival.id,
            source_use="production",
            priority=1,
            valid_from=date(2026, 1, 1),
        )
        # Demote the dongle to a fallback.
        from nemsei.sources.models import AssetSourcePolicy

        policy = session.scalar(
            select(AssetSourcePolicy).where(AssetSourcePolicy.provider_mapping_id == ids["mapping"])
        )
        policy.is_fallback = True

    result = roll(factory, settings, ids)

    assert facts(factory) == []
    assert result.skipped_reasons.get("not_the_selected_production_source") == 1


def test_an_asset_without_a_timezone_is_skipped_loudly_not_defaulted(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    from nemsei.assets.models import Asset

    with factory() as session, session.begin():
        session.get(Asset, ids["asset"]).timezone = None

    result = roll(factory, settings, ids)

    assert result.skipped_reasons.get("asset_timezone_missing") == 1
    assert facts(factory) == []


def test_only_the_newest_revision_of_a_sample_is_integrated(world, monkeypatch) -> None:
    """Append-only samples: totalling the raw rows would count a correction twice."""
    factory, ids = world
    moment = datetime(2026, 8, 24, 12, 0, tzinfo=datetime_timezone.utc)
    with factory() as session, session.begin():
        for revision, value in ((1, Decimal("10")), (2, Decimal("20"))):
            session.add(
                HuaweiScadaPowerSample(
                    asset_id=ids["asset"],
                    provider_mapping_id=ids["mapping"],
                    dongle_serial=SERIAL,
                    source_sample_key="huawei-scada:same-instant",
                    source_revision=revision,
                    observed_at=moment,
                    ingested_at=moment,
                    total_active_power_kw=value,
                    raw_registers_json={},
                    quality="complete",
                    completeness="complete",
                    session_state="polling",
                    metadata_json={},
                )
            )

    with factory() as session:
        rows = current_samples(
            session,
            provider_mapping_id=ids["mapping"],
            period_start=moment - timedelta(hours=1),
            period_end=moment + timedelta(hours=1),
        )
    assert [row.source_revision for row in rows] == [2]


# --- retention ----------------------------------------------------------------


def test_retention_deletes_only_days_whose_energy_is_already_final(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)
    roll(factory, settings, ids)

    # Long after the fact: the day is settled and its samples are redundant.
    result = purge_samples(
        factory,
        connection_id=ids["connection"],
        retention_days=30,
        now=datetime(2026, 12, 1, tzinfo=datetime_timezone.utc),
    )

    assert result.samples_deleted == 288
    with factory() as session:
        assert session.scalar(select(HuaweiScadaPowerSample)) is None
        # The energy survives; only the raw evidence behind it is reclaimed.
        assert session.scalar(select(ProductionFact)) is not None


def test_retention_leaves_a_day_whose_energy_is_only_partial(world, settings, monkeypatch) -> None:
    """A partial day may still be improved by a late sample."""
    factory, ids = world
    configured(monkeypatch)
    add_series(factory, ids, start_local=datetime(2026, 8, 24, 9, 0, tzinfo=LISBON), count=96)
    roll(factory, settings, ids)
    assert facts(factory, metric="production_energy")[0].completeness == "partial"

    result = purge_samples(
        factory,
        connection_id=ids["connection"],
        retention_days=30,
        now=datetime(2026, 12, 1, tzinfo=datetime_timezone.utc),
    )

    assert result.samples_deleted == 0
    with factory() as session:
        assert session.scalar(select(HuaweiScadaPowerSample)) is not None


def test_retention_never_touches_a_day_inside_the_retention_window(world, settings, monkeypatch) -> None:
    factory, ids = world
    configured(monkeypatch)
    full_day(factory, ids)
    roll(factory, settings, ids)

    result = purge_samples(
        factory,
        connection_id=ids["connection"],
        retention_days=90,
        now=datetime(2026, 8, 25, tzinfo=datetime_timezone.utc),
    )

    assert result.samples_deleted == 0


def test_retention_dismantles_the_supersession_chain_deliberately(world, settings, monkeypatch) -> None:
    """`supersedes_sample_id` is RESTRICT, so a whole-window delete has to say so."""
    factory, ids = world
    configured(monkeypatch)
    moment = datetime(2026, 8, 24, 12, 0, tzinfo=datetime_timezone.utc)
    with factory() as session, session.begin():
        first = HuaweiScadaPowerSample(
            asset_id=ids["asset"], provider_mapping_id=ids["mapping"], dongle_serial=SERIAL,
            source_sample_key="k", source_revision=1, observed_at=moment, ingested_at=moment,
            total_active_power_kw=Decimal("10"), raw_registers_json={}, quality="complete",
            completeness="complete", session_state="polling", metadata_json={},
        )
        session.add(first)
        session.flush()
        session.add(
            HuaweiScadaPowerSample(
                asset_id=ids["asset"], provider_mapping_id=ids["mapping"], dongle_serial=SERIAL,
                source_sample_key="k", source_revision=2, supersedes_sample_id=first.id,
                observed_at=moment, ingested_at=moment, total_active_power_kw=Decimal("11"),
                raw_registers_json={}, quality="complete", completeness="complete",
                session_state="polling", metadata_json={},
            )
        )
    full_day(factory, ids)
    roll(factory, settings, ids)

    result = purge_samples(
        factory,
        connection_id=ids["connection"],
        retention_days=30,
        now=datetime(2026, 12, 1, tzinfo=datetime_timezone.utc),
    )

    assert result.samples_deleted >= 2
    with factory() as session:
        assert session.scalar(select(HuaweiScadaPowerSample)) is None
