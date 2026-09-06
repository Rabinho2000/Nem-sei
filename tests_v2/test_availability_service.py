"""Database round-trip for the availability materializer -- item 5/7 coverage.

Proves the whole chain against a real Postgres: `device_status_facts` (as
`FusionSolarDeviceStatusService` already writes them) in, `device_
availability_daily`/`asset_availability_daily` out, idempotent re-run, and
zero provider calls anywhere on the path (no client/credentials touched by
this module at all -- see `availability_service.py`'s own docstring).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from nemsei.assets.service import create_asset, create_device
from nemsei.installations.models import Installation
from nemsei.shared.clock import utc_now
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.availability_service import (
    expected_devices_for_date,
    installation_availability_for_date,
    materialize_asset_availability_day,
    materialize_existing_availability,
    monthly_availability_for_asset,
    portfolio_availability_for_date,
)
from nemsei.diagnostics.models import AssetAvailabilityDaily, DeviceAvailabilityDaily
from nemsei.diagnostics.service import record_device_status
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.rules.availability_window import COVERAGE_COMPLETE, LISBON, rollup_availability
from tests_v2.test_migrations import upgrade


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _lisbon(hour: int, minute: int = 0, *, day: int = 15) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=LISBON)


_FIXTURE_VALID_FROM = date(2026, 1, 1)
_connection_counter = 0


def _build_asset_with_two_inverters(session, *, provider_code="fusionsolar", installed_dc_power_kw=None):
    global _connection_counter
    _connection_counter += 1
    connection = create_connection(
        session, provider_code=provider_code, connection_key=f"conn-{provider_code}-{_connection_counter}", display_name="Fixture connection",
        credential_reference="dev", enabled=True, configuration_status="configured",
    )
    asset = create_asset(session, canonical_name="Availability fixture plant", installed_dc_power_kw=installed_dc_power_kw)
    create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="ST-1", valid_from=_FIXTURE_VALID_FROM)
    devices = []
    for number, power in enumerate((Decimal("20.0"), Decimal("30.0")), start=1):
        device = create_device(
            session, asset_id=asset.id, device_kind="inverter", serial_number=f"SN-{number}", rated_power_kw=power,
            valid_from=_FIXTURE_VALID_FROM,
        )
        create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"DEV-{number}",
            resource_kind="device", device_id=device.id, valid_from=_FIXTURE_VALID_FROM,
        )
        devices.append(device)
    session.commit()
    return asset, devices


def _record(session, *, device, asset_id, when, power, status="available"):
    record_device_status(
        session, device_id=device.id, asset_id=asset_id, source_fact_key=f"fusionsolar-device-live:{device.id}",
        observed_at=when, availability_status=status, active_power_kw=power, day_energy_kwh=Decimal("1.0"),
        source_kind="live_read", freshness="unknown", quality="complete", completeness="complete",
    )


def test_expected_devices_only_lists_active_fusionsolar_inverter_mappings(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, devices = _build_asset_with_two_inverters(session)
        expected = expected_devices_for_date(session, asset_id=asset.id, target_date=date(2026, 7, 15))
        assert {row["device_id"] for row in expected} == {device.id for device in devices}


def test_sigenergy_asset_has_no_expected_devices(settings, monkeypatch):
    """Item 9: Sigenergy stays blocked structurally, not by a special-cased skip."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, _devices = _build_asset_with_two_inverters(session, provider_code="sigenergy")
        expected = expected_devices_for_date(session, asset_id=asset.id, target_date=date(2026, 7, 15))
        assert expected == []


def test_materialize_persists_a_complete_day_and_weights_by_rated_power(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, (device_1, device_2) = _build_asset_with_two_inverters(session)
        hour = 6
        while hour <= 20:
            for device in (device_1, device_2):
                _record(session, device=device, asset_id=asset.id, when=_lisbon(hour), power=Decimal("5.0"))
            hour += 1
        session.commit()

        result = materialize_asset_availability_day(session, asset_id=asset.id, target_date=date(2026, 7, 15))
        session.commit()

        assert result.coverage_status == COVERAGE_COMPLETE
        assert result.availability_pct == 100.0

        stored_asset = session.query(AssetAvailabilityDaily).filter_by(asset_id=asset.id, availability_date=date(2026, 7, 15)).one()
        assert stored_asset.coverage_status == "complete"
        assert float(stored_asset.availability_pct) == 100.0
        device_rows = session.query(DeviceAvailabilityDaily).filter_by(asset_id=asset.id, availability_date=date(2026, 7, 15)).all()
        assert len(device_rows) == 2
        assert {row.device_id for row in device_rows} == {device_1.id, device_2.id}


def test_materialize_is_idempotent_on_rerun(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, (device_1, device_2) = _build_asset_with_two_inverters(session)
        for hour in range(6, 21):
            for device in (device_1, device_2):
                _record(session, device=device, asset_id=asset.id, when=_lisbon(hour), power=Decimal("5.0"))
        session.commit()

        materialize_asset_availability_day(session, asset_id=asset.id, target_date=date(2026, 7, 15))
        session.commit()
        materialize_asset_availability_day(session, asset_id=asset.id, target_date=date(2026, 7, 15))
        session.commit()

        assert session.query(AssetAvailabilityDaily).filter_by(asset_id=asset.id, availability_date=date(2026, 7, 15)).count() == 1
        assert session.query(DeviceAvailabilityDaily).filter_by(asset_id=asset.id, availability_date=date(2026, 7, 15)).count() == 2


def test_materialize_never_touches_v1_import_facts(settings, monkeypatch):
    """Only `source_kind='live_read'` feeds the window -- V1 backfill rows are excluded."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, (device_1, device_2) = _build_asset_with_two_inverters(session)
        # A v1_import row, sparse and alone, must not be able to open an
        # operating window by itself.
        record_device_status(
            session, device_id=device_1.id, asset_id=asset.id, source_fact_key=f"v1-import:{device_1.id}",
            observed_at=_lisbon(12), availability_status="available", active_power_kw=Decimal("5.0"),
            source_kind="v1_import",
        )
        session.commit()
        result = materialize_asset_availability_day(session, asset_id=asset.id, target_date=date(2026, 7, 15))
        assert result.coverage_status == "missing"


def test_materialize_existing_availability_recomputes_a_range_with_zero_provider_calls(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, (device_1, device_2) = _build_asset_with_two_inverters(session)
        for day in (14, 15):
            for hour in range(6, 21):
                for device in (device_1, device_2):
                    _record(session, device=device, asset_id=asset.id, when=_lisbon(hour, day=day), power=Decimal("5.0"))
        session.commit()

        summary = materialize_existing_availability(session, asset_id=asset.id, from_date=date(2026, 7, 14), to_date=date(2026, 7, 15))
        session.commit()

        assert summary["days_recalculated"] == 2
        assert summary.get("complete") == 2
        assert session.query(AssetAvailabilityDaily).filter_by(asset_id=asset.id).count() == 2


def test_installation_rollup_is_none_when_one_member_asset_is_incomplete(settings, monkeypatch):
    """Item 5: an installation spanning multiple assets, one incomplete -> the whole installation is None."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset_a, (device_a1, device_a2) = _build_asset_with_two_inverters(session, installed_dc_power_kw=Decimal("500"))
        for hour in range(6, 21):
            for device in (device_a1, device_a2):
                _record(session, device=device, asset_id=asset_a.id, when=_lisbon(hour), power=Decimal("5.0"))
        asset_b, (device_b1, device_b2) = _build_asset_with_two_inverters(session, installed_dc_power_kw=Decimal("300"))
        _record(session, device=device_b1, asset_id=asset_b.id, when=_lisbon(6), power=Decimal("5.0"))  # far too sparse
        session.commit()

        result_a = materialize_asset_availability_day(session, asset_id=asset_a.id, target_date=date(2026, 7, 15))
        result_b = materialize_asset_availability_day(session, asset_id=asset_b.id, target_date=date(2026, 7, 15))
        session.commit()

        assert result_a.coverage_status == "complete"
        assert result_b.coverage_status != "complete"

        rollup_rows = [
            {"availability_pct": result_a.availability_pct, "installed_dc_power_kw": float(asset_a.installed_dc_power_kw)},
            {"availability_pct": result_b.availability_pct, "installed_dc_power_kw": float(asset_b.installed_dc_power_kw)},
        ]
        assert rollup_availability(rollup_rows, weight_key="installed_dc_power_kw") is None


def test_installation_availability_for_date_weights_member_assets(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset_a, (device_a1, device_a2) = _build_asset_with_two_inverters(session, installed_dc_power_kw=Decimal("90"))
        asset_b, (device_b1, device_b2) = _build_asset_with_two_inverters(session, installed_dc_power_kw=Decimal("10"))
        for hour in range(6, 21):
            for device in (device_a1, device_a2, device_b1, device_b2):
                power = Decimal("5.0")
                asset_id = asset_a.id if device in (device_a1, device_a2) else asset_b.id
                status = "unavailable" if (device is device_b1 and hour < 14) else "available"
                _record(session, device=device, asset_id=asset_id, when=_lisbon(hour), power=power, status=status)

        now = utc_now()
        installation = Installation(display_name="Fixture installation", created_at=now, updated_at=now)
        session.add(installation)
        session.flush()
        asset_a.installation_id = installation.id
        asset_b.installation_id = installation.id
        session.commit()

        materialize_asset_availability_day(session, asset_id=asset_a.id, target_date=date(2026, 7, 15))
        materialize_asset_availability_day(session, asset_id=asset_b.id, target_date=date(2026, 7, 15))
        session.commit()

        rolled_up = installation_availability_for_date(session, installation_id=installation.id, target_date=date(2026, 7, 15))
        assert rolled_up is not None
        # asset_a: 100% (weight 90), asset_b: <100% (weight 10) -> weighted mean is high but not 100.
        assert 90.0 < rolled_up < 100.0


def test_installation_availability_is_none_with_no_materialized_days(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, _devices = _build_asset_with_two_inverters(session)
        now = utc_now()
        installation = Installation(display_name="Empty installation", created_at=now, updated_at=now)
        session.add(installation)
        session.flush()
        asset.installation_id = installation.id
        session.commit()
        assert installation_availability_for_date(session, installation_id=installation.id, target_date=date(2026, 7, 15)) is None


def test_portfolio_availability_is_none_with_no_members(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        from nemsei.portfolios.service import create_portfolio

        portfolio = create_portfolio(session, name="Empty fixture portfolio", created_by="test-fixture")
        session.commit()
        assert portfolio_availability_for_date(session, portfolio_id=portfolio.id, target_date=date(2026, 7, 15)) is None


# ---------------------------------------------------------------------------
# Monthly aggregation (item 3), ported from V1's `sampled_month_quality`.
# The one intentional deviation (zero materialized days -> 'missing', not
# V1's own 'sampled_partial' for that same case -- see this function's own
# docstring) is pinned explicitly below, not left implicit.
# ---------------------------------------------------------------------------


def _materialize_month(session, *, asset_id, devices, month_start: date, days: int, complete: bool = True):
    for offset in range(days):
        day = date.fromordinal(month_start.toordinal() + offset)
        for hour in range(6, 21):
            for device in devices:
                # An incomplete month: one device never reports at all on the
                # very last day -- `missing_expected_inverter`, which makes
                # that one day (and so the whole month) `partial`. Truncating
                # the last hours instead would only shrink that day's own
                # observed window and could still pass every gap/tolerance
                # check trivially -- this is the genuinely broken case.
                if not complete and offset == days - 1 and device is devices[-1]:
                    continue
                _record(session, device=device, asset_id=asset_id, when=_lisbon(hour, day=day.day), power=Decimal("5.0"))
        materialize_asset_availability_day(session, asset_id=asset_id, target_date=day)
    session.commit()


def test_monthly_availability_is_complete_when_every_day_is(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, devices = _build_asset_with_two_inverters(session)
        _materialize_month(session, asset_id=asset.id, devices=devices, month_start=date(2026, 7, 1), days=31, complete=True)
        result = monthly_availability_for_asset(session, asset_id=asset.id, month_start=date(2026, 7, 1), month_end_exclusive=date(2026, 8, 1))
        assert result["coverage_status"] == "complete"
        assert result["availability_pct"] == 100.0
        assert result["covered_days"] == 31
        assert result["expected_days"] == 31


def test_monthly_availability_is_partial_when_one_day_is_incomplete(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, devices = _build_asset_with_two_inverters(session)
        _materialize_month(session, asset_id=asset.id, devices=devices, month_start=date(2026, 7, 1), days=31, complete=False)
        result = monthly_availability_for_asset(session, asset_id=asset.id, month_start=date(2026, 7, 1), month_end_exclusive=date(2026, 8, 1))
        assert result["coverage_status"] == "partial"
        assert result["availability_pct"] is None  # No number reported for a non-final month, ever.
        assert result["covered_days"] == 31  # Every day materialized -- just not every day complete.


def test_monthly_availability_is_missing_with_zero_materialized_days(settings, monkeypatch):
    """Deliberate improvement over V1: V1's own function calls this 'sampled_partial' too."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset, _devices = _build_asset_with_two_inverters(session)
        result = monthly_availability_for_asset(session, asset_id=asset.id, month_start=date(2026, 7, 1), month_end_exclusive=date(2026, 8, 1))
        assert result["coverage_status"] == "missing"
        assert result["availability_pct"] is None
        assert result["covered_days"] == 0
        assert result["expected_days"] == 31
