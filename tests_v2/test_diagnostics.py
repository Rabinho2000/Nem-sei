"""Device status facts: append-only, anchored on canonical device identity."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.rules import classify_fusionsolar_inverter_availability
from nemsei.diagnostics.service import current_device_status, record_device_status


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int = 12) -> datetime:
    return datetime(2026, 7, 24, hour, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def asset_and_device(factory):
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        ids = (asset.id, device.id)
    return factory, ids


# --- the ported classification -----------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (512, "available"),
        (2048, "available"),
        (768, "unavailable"),
        (774, "unavailable"),
        (0, "standby"),
        (1792, "standby"),
        (40960, "unknown"),
        (None, "unknown"),
        ("not-a-number", "unknown"),
    ],
)
def test_classification_covers_every_named_set_and_falls_back_to_unknown(state, expected) -> None:
    assert classify_fusionsolar_inverter_availability(state) == expected


def test_no_recent_data_overrides_an_otherwise_available_state() -> None:
    assert classify_fusionsolar_inverter_availability(512, has_recent_data=False) == "unknown"


def test_a_critical_alarm_overrides_an_otherwise_available_state() -> None:
    assert classify_fusionsolar_inverter_availability(512, has_critical_alarm=True) == "unavailable"


# --- persistence ---------------------------------------------------------------


def test_recording_the_same_reading_twice_creates_nothing_the_second_time(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        _fact, created_first = record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(), availability_status="available", active_power_kw=Decimal("4.2"),
        )
        _fact2, created_second = record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(), availability_status="available", active_power_kw=Decimal("4.2"),
        )
    assert created_first is True
    assert created_second is False


def test_a_changed_reading_under_the_same_key_supersedes_rather_than_duplicates(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        first, _ = record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(), availability_status="unavailable",
        )
        second, created = record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(), availability_status="available", active_power_kw=Decimal("3.1"),
        )
    assert created is True
    assert second.supersedes_fact_id == first.id
    assert second.source_revision == 2


def test_a_device_status_fact_must_belong_to_its_own_asset(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        other = create_asset(session, canonical_name="Somewhere Else")
        other_id = other.id
    with pytest.raises(ValueError, match="own asset"):
        with factory() as session, session.begin():
            record_device_status(
                session, device_id=device_id, asset_id=other_id, source_fact_key="v1:1",
                observed_at=utc(), availability_status="available",
            )


def test_an_unknown_availability_status_is_refused(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with pytest.raises(ValueError, match="Invalid device availability status"):
        with factory() as session, session.begin():
            record_device_status(
                session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
                observed_at=utc(), availability_status="fault",
            )


def test_a_negative_power_reading_is_rejected_by_the_database(asset_and_device) -> None:
    """The check constraint, not just the service layer: a negative reading is
    a parse artefact, never a real state a device can be in."""
    factory, (asset_id, device_id) = asset_and_device
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            record_device_status(
                session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
                observed_at=utc(), availability_status="available", active_power_kw=Decimal("-1"),
            )


# --- reading current status ----------------------------------------------------


def test_current_status_lists_every_device_including_one_with_no_reading(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Beta Solar")
        reporting = create_device(session, asset_id=asset.id, device_kind="inverter", label="Reports", valid_from=date(2026, 1, 1))
        # No reading is ever recorded for this one — its absence is the point.
        create_device(session, asset_id=asset.id, device_kind="inverter", label="Silent", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=reporting.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=utc(), availability_status="available", active_power_kw=Decimal("5.5"),
        )
        asset_id = asset.id

    with factory() as session:
        rows = {row["label"]: row for row in current_device_status(session, asset_id=asset_id)}

    assert rows["Reports"]["has_reading"] is True
    assert rows["Reports"]["availability_status"] == "available"
    assert rows["Reports"]["active_power_kw"] == Decimal("5.500")
    assert rows["Silent"]["has_reading"] is False
    assert rows["Silent"]["availability_status"] == "unknown"
    assert rows["Silent"]["observed_at"] is None


def test_current_status_reports_the_most_recent_reading_not_the_first(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(hour=8), availability_status="standby", active_power_kw=Decimal("0"),
        )
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:2",
            observed_at=utc(hour=14), availability_status="available", active_power_kw=Decimal("6.0"),
        )

    with factory() as session:
        rows = current_device_status(session, asset_id=asset_id)
    assert len(rows) == 1
    assert rows[0]["availability_status"] == "available"
    assert rows[0]["active_power_kw"] == Decimal("6.000")
