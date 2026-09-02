"""Energy/financial impact (Telegram O&M redesign, req 6/17): real data only,
`None` ("não calculável") is never `0` unless the zero was itself calculated
(fully outside the productive window).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DeviceStatusFact
from nemsei.notifications.impact import estimate_energy_impact, estimate_financial_impact
from nemsei.reporting.commercial import representative_price, resolve_tariff, set_tariff

# Lisbon -- clearly daylight at noon UTC and clearly dark at 02:00 UTC, in July.
LISBON_LAT = Decimal("38.7223")
LISBON_LON = Decimal("-9.1393")


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def rig(factory):
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="DIACO")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        return asset.id, device.id


# --- no coordinates -> unknown, never zero --------------------------------------


def test_no_coordinates_is_not_calculable(factory, rig) -> None:
    asset_id, device_id = rig
    with factory() as session:
        result = estimate_energy_impact(
            session, asset_id=asset_id, device_id=device_id, start=utc(10), end=utc(12),
            latitude=None, longitude=None,
        )
    assert result.lost_kwh is None
    assert "coordenadas" in result.basis


# --- entirely outside the productive window is a real, calculated zero --------


def test_entirely_outside_the_productive_window_is_a_real_zero(factory, rig) -> None:
    asset_id, device_id = rig
    with factory() as session:
        result = estimate_energy_impact(
            session, asset_id=asset_id, device_id=device_id, start=utc(2), end=utc(3),
            latitude=LISBON_LAT, longitude=LISBON_LON,
        )
    assert result.lost_kwh == Decimal("0")
    assert "período produtivo" in result.basis


# --- productive window, no recent reading -> not calculable --------------------


def test_no_recent_power_reading_is_not_calculable(factory, rig) -> None:
    asset_id, device_id = rig
    with factory() as session:
        result = estimate_energy_impact(
            session, asset_id=asset_id, device_id=device_id, start=utc(10), end=utc(12),
            latitude=LISBON_LAT, longitude=LISBON_LON,
        )
    assert result.lost_kwh is None
    assert "leitura de potência" in result.basis


# --- productive window with a recent reading -> a real estimate ----------------


def test_a_recent_reading_produces_a_real_estimate(factory, rig) -> None:
    asset_id, device_id = rig
    with factory() as session, session.begin():
        session.add(
            DeviceStatusFact(
                device_id=device_id, asset_id=asset_id, source_fact_key="k1", source_revision=1,
                observed_at=utc(9, 30), ingested_at=utc(9, 30), availability_status="available",
                active_power_kw=Decimal("100"), source_kind="live_read",
            )
        )

    with factory() as session:
        result = estimate_energy_impact(
            session, asset_id=asset_id, device_id=device_id, start=utc(10), end=utc(12),
            latitude=LISBON_LAT, longitude=LISBON_LON,
        )
    assert result.lost_kwh == Decimal("200.0")  # 100kW x 2h, fully inside the productive window


def test_a_reading_more_than_two_hours_before_the_outage_is_ignored(factory, rig) -> None:
    asset_id, device_id = rig
    with factory() as session, session.begin():
        session.add(
            DeviceStatusFact(
                device_id=device_id, asset_id=asset_id, source_fact_key="k1", source_revision=1,
                observed_at=utc(6, 0), ingested_at=utc(6, 0), availability_status="available",
                active_power_kw=Decimal("100"), source_kind="live_read",
            )
        )

    with factory() as session:
        result = estimate_energy_impact(
            session, asset_id=asset_id, device_id=device_id, start=utc(10), end=utc(12),
            latitude=LISBON_LAT, longitude=LISBON_LON,
        )
    assert result.lost_kwh is None


# --- asset-level (device_id=None) sums every device's last reading -------------


def test_asset_level_impact_sums_every_device(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Multi-inverter plant")
        d1 = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        d2 = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-2", valid_from=date(2026, 1, 1))
        for device, power in ((d1, Decimal("60")), (d2, Decimal("40"))):
            session.add(
                DeviceStatusFact(
                    device_id=device.id, asset_id=asset.id, source_fact_key=f"k{device.id}", source_revision=1,
                    observed_at=utc(9, 30), ingested_at=utc(9, 30), availability_status="available",
                    active_power_kw=power, source_kind="live_read",
                )
            )
        asset_id = asset.id

    with factory() as session:
        result = estimate_energy_impact(
            session, asset_id=asset_id, device_id=None, start=utc(10), end=utc(11),
            latitude=LISBON_LAT, longitude=LISBON_LON,
        )
    assert result.lost_kwh == Decimal("100.0")  # (60+40)kW x 1h


# --- financial impact: no tariff -> unknown, never zero -------------------------


def test_no_tariff_is_not_calculable_even_with_a_real_energy_estimate(factory, rig) -> None:
    asset_id, device_id = rig
    with factory() as session, session.begin():
        session.add(
            DeviceStatusFact(
                device_id=device_id, asset_id=asset_id, source_fact_key="k1", source_revision=1,
                observed_at=utc(9, 30), ingested_at=utc(9, 30), availability_status="available",
                active_power_kw=Decimal("100"), source_kind="live_read",
            )
        )

    with factory() as session:
        energy = estimate_energy_impact(
            session, asset_id=asset_id, device_id=device_id, start=utc(10), end=utc(12),
            latitude=LISBON_LAT, longitude=LISBON_LON,
        )
        tariff = resolve_tariff(session, asset_id=asset_id, on=date(2026, 7, 24))
        price = representative_price(tariff) if tariff else None
        financial = estimate_financial_impact(energy=energy, price_eur_per_kwh=price)

    assert energy.lost_kwh == Decimal("200.0")
    assert financial.lost_eur is None
    assert "tarifa" in financial.basis


def test_a_configured_tariff_produces_a_real_financial_estimate(factory, rig) -> None:
    asset_id, device_id = rig
    with factory() as session, session.begin():
        session.add(
            DeviceStatusFact(
                device_id=device_id, asset_id=asset_id, source_fact_key="k1", source_revision=1,
                observed_at=utc(9, 30), ingested_at=utc(9, 30), availability_status="available",
                active_power_kw=Decimal("100"), source_kind="live_read",
            )
        )
        set_tariff(
            session, asset_id=asset_id, tariff_type="simple", valid_from=date(2026, 1, 1), created_by="ops",
            prices={"simple": Decimal("0.15")},
        )

    with factory() as session:
        energy = estimate_energy_impact(
            session, asset_id=asset_id, device_id=device_id, start=utc(10), end=utc(12),
            latitude=LISBON_LAT, longitude=LISBON_LON,
        )
        tariff = resolve_tariff(session, asset_id=asset_id, on=date(2026, 7, 24))
        price = representative_price(tariff)
        financial = estimate_financial_impact(energy=energy, price_eur_per_kwh=price)

    assert financial.lost_eur == Decimal("30.00")  # 200kWh x 0.15€/kWh
