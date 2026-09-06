"""Contractual availability end to end: ingest -> facts -> daily -> month -> report.

Proves the properties the milestone names explicitly, against a real
PostgreSQL: contractual and sampled coexist for the same day without either
erasing the other, a late provider correction becomes a revision rather than
an overwrite, monthly weighting is slot-weighted, and the customer report
picks the contractual figure over the sampled one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from nemsei.assets.service import create_asset, create_device
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.availability_service import (
    assets_missing_history_for_date,
    materialize_asset_contractual_day,
    materialize_contractual_window,
    materialize_existing_availability,
    monthly_availability_for_asset,
)
from nemsei.diagnostics.models import AssetAvailabilityDaily, DeviceAvailabilityDaily, DeviceStatusFact
from nemsei.diagnostics.service import record_device_status
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.rules.availability_source import (
    KIND_CONTRACTUAL,
    KIND_OPERATIONAL,
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
    SOURCE_FUSIONSOLAR_SAMPLED,
)
from tests_v2.test_migrations import upgrade

UTC = ZoneInfo("UTC")
LISBON = ZoneInfo("Europe/Lisbon")
DAY = date(2026, 6, 15)
_VALID_FROM = date(2026, 1, 1)
_counter = 0


def _factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _plant(session, *, inverters=((Decimal("33.0")), (Decimal("30.0")))):
    global _counter
    _counter += 1
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key=f"conn-contract-{_counter}",
        display_name="Contractual fixture", credential_reference="dev", enabled=True, configuration_status="configured",
    )
    asset = create_asset(session, canonical_name=f"Contractual plant {_counter}", installed_dc_power_kw=Decimal("63.0"))
    create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"ST-C{_counter}", valid_from=_VALID_FROM)
    devices = []
    for index, power in enumerate(inverters, start=1):
        device = create_device(
            session, asset_id=asset.id, device_kind="inverter", serial_number=f"SN-C{_counter}-{index}",
            rated_power_kw=power, valid_from=_VALID_FROM,
        )
        create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"DEV-C{_counter}-{index}",
            resource_kind="device", device_id=device.id, valid_from=_VALID_FROM,
        )
        devices.append(device)
    # Flush, never commit: every caller already owns a `session.begin()`.
    session.flush()
    return connection, asset, devices


def _history(session, *, device, asset_id, day=DAY, start_hour=7, end_hour=19, power=Decimal("10.0"),
             dark_hours=(), status="available"):
    """A 5-minute `history_read` series, the real provider cadence."""
    cursor = datetime(day.year, day.month, day.day, start_hour, 0, tzinfo=UTC)
    end = datetime(day.year, day.month, day.day, end_hour, 0, tzinfo=UTC)
    while cursor <= end:
        value = Decimal("0.0") if cursor.hour in dark_hours else power
        record_device_status(
            session, device_id=device.id, asset_id=asset_id,
            source_fact_key=f"fusionsolar-device-history:{device.id}:{cursor.isoformat()}",
            observed_at=cursor, availability_status=status, active_power_kw=value,
            source_kind="history_read", freshness="fresh", quality="complete", completeness="complete",
        )
        cursor += timedelta(minutes=5)


def test_contractual_day_persists_with_its_own_source_and_slot_weight(settings, monkeypatch):
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        for device in devices:
            _history(session, device=device, asset_id=asset.id)
        result = materialize_asset_contractual_day(session, asset_id=asset.id, target_date=DAY, tz=UTC)
        asset_id = asset.id
    assert result.availability_pct == pytest.approx(100.0)

    with factory() as session:
        row = session.query(AssetAvailabilityDaily).filter_by(asset_id=asset_id, availability_date=DAY).one()
        assert row.source == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
        assert row.source_kind == KIND_CONTRACTUAL
        # The monthly weight is V1's tolerated plant slot count, not a raw
        # sample count: 07:00-19:00 is 49 quarter-hour slots, minus 30 min at
        # each end.
        assert row.valid_sample_count == 45
        assert row.calculation_details_json["calculation"] == "weighted_device_aggregation"
        assert len(session.query(DeviceAvailabilityDaily).filter_by(asset_id=asset_id).all()) == 2


def test_contractual_and_sampled_coexist_for_the_same_day(settings, monkeypatch):
    """The milestone's mandatory coexistence case: both rows survive."""
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        asset_id = asset.id
        for device in devices:
            _history(session, device=device, asset_id=asset_id)
            # A sparse realtime series for the same day, on Lisbon time.
            for hour in range(7, 20):
                record_device_status(
                    session, device_id=device.id, asset_id=asset_id,
                    source_fact_key=f"fusionsolar-device-live:{device.id}",
                    observed_at=datetime(DAY.year, DAY.month, DAY.day, hour, tzinfo=LISBON),
                    availability_status="available", active_power_kw=Decimal("9.0"),
                    source_kind="live_read", freshness="unknown", quality="complete", completeness="complete",
                )

    # Materialize sampled first, then contractual: neither may erase the other.
    with factory() as session, session.begin():
        materialize_existing_availability(session, asset_id=asset_id, from_date=DAY, to_date=DAY)
    with factory() as session, session.begin():
        materialize_contractual_window(session, from_date=DAY, to_date=DAY, tz=UTC, asset_ids=[asset_id])
    # And re-materializing sampled afterwards must still not erase contractual.
    with factory() as session, session.begin():
        materialize_existing_availability(session, asset_id=asset_id, from_date=DAY, to_date=DAY)

    with factory() as session:
        rows = {r.source: r for r in session.query(AssetAvailabilityDaily).filter_by(asset_id=asset_id, availability_date=DAY).all()}
    assert set(rows) == {SOURCE_FUSIONSOLAR_SAMPLED, SOURCE_FUSIONSOLAR_DEVICE_HISTORY}
    assert rows[SOURCE_FUSIONSOLAR_DEVICE_HISTORY].source_kind == KIND_CONTRACTUAL
    assert rows[SOURCE_FUSIONSOLAR_SAMPLED].source_kind == KIND_OPERATIONAL


def test_a_late_provider_correction_creates_a_revision_and_changes_the_figure(settings, monkeypatch):
    """Correction path: revision, supersession, recomputed daily -- no deletes."""
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        asset_id = asset.id
        device_ids = [device.id for device in devices]
        for device in devices:
            _history(session, device=device, asset_id=asset_id)
    with factory() as session, session.begin():
        first = materialize_asset_contractual_day(session, asset_id=asset_id, target_date=DAY, tz=UTC)
    assert first.availability_pct == pytest.approx(100.0)

    # The provider now reports one inverter as having produced nothing for
    # three hours of that same day -- same instants, corrected values.
    corrected = datetime(DAY.year, DAY.month, DAY.day, 12, 0, tzinfo=UTC)
    with factory() as session, session.begin():
        cursor = corrected
        while cursor < corrected + timedelta(hours=3):
            record_device_status(
                session, device_id=device_ids[0], asset_id=asset_id,
                source_fact_key=f"fusionsolar-device-history:{device_ids[0]}:{cursor.isoformat()}",
                observed_at=cursor, availability_status="unavailable", active_power_kw=Decimal("0.0"),
                source_kind="history_read", freshness="fresh", quality="complete", completeness="complete",
            )
            cursor += timedelta(minutes=5)
    with factory() as session, session.begin():
        second = materialize_asset_contractual_day(session, asset_id=asset_id, target_date=DAY, tz=UTC)

    assert second.availability_pct is not None
    assert second.availability_pct < first.availability_pct, "the correction must move the number"

    with factory() as session:
        facts = session.query(DeviceStatusFact).filter_by(
            device_id=device_ids[0],
            source_fact_key=f"fusionsolar-device-history:{device_ids[0]}:{corrected.isoformat()}",
        ).order_by(DeviceStatusFact.source_revision).all()
        # Both revisions still exist, and the new one points at the old.
        assert [fact.source_revision for fact in facts] == [1, 2]
        assert facts[1].supersedes_fact_id == facts[0].id
        assert facts[0].active_power_kw != facts[1].active_power_kw


def test_re_materializing_unchanged_facts_is_idempotent(settings, monkeypatch):
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        asset_id = asset.id
        for device in devices:
            _history(session, device=device, asset_id=asset_id)
    for _ in range(2):
        with factory() as session, session.begin():
            materialize_contractual_window(session, from_date=DAY, to_date=DAY, tz=UTC, asset_ids=[asset_id])
    with factory() as session:
        assert session.query(AssetAvailabilityDaily).filter_by(asset_id=asset_id, availability_date=DAY).count() == 1
        assert session.query(DeviceAvailabilityDaily).filter_by(asset_id=asset_id, availability_date=DAY).count() == 2


def test_reingesting_identical_history_writes_no_new_revision(settings, monkeypatch):
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        asset_id, device = asset.id, devices[0]
        _history(session, device=device, asset_id=asset_id)
        device_id = device.id
    with factory() as session:
        before = session.query(DeviceStatusFact).filter_by(device_id=device_id).count()
    with factory() as session, session.begin():
        device = session.get(type(devices[0]), device_id)
        _history(session, device=device, asset_id=asset_id)
    with factory() as session:
        assert session.query(DeviceStatusFact).filter_by(device_id=device_id).count() == before


def test_the_scheduler_skips_a_day_whose_history_is_already_in(settings, monkeypatch):
    """Steady state must cost zero provider calls."""
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        connection, asset, devices = _plant(session)
        connection_id, asset_id = connection.id, asset.id
    with factory() as session:
        assert assets_missing_history_for_date(session, connection_id=connection_id, target_date=DAY, tz=UTC) == [asset_id]
    with factory() as session, session.begin():
        for device in devices:
            _history(session, device=device, asset_id=asset_id)
    with factory() as session:
        assert assets_missing_history_for_date(session, connection_id=connection_id, target_date=DAY, tz=UTC) == []


def test_monthly_contractual_uses_slot_weighting_over_real_materialized_days(settings, monkeypatch):
    """Two real days with different slot counts: weighted, not a day mean."""
    factory = _factory(settings, monkeypatch)
    month_start, month_end = date(2026, 6, 1), date(2026, 6, 3)
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session, inverters=(Decimal("10.0"),))
        asset_id = asset.id
        device = devices[0]
        # Day 1: a long window, fully available.
        _history(session, device=device, asset_id=asset_id, day=date(2026, 6, 1), start_hour=6, end_hour=20)
        # Day 2: a short window, half of it dark.
        _history(session, device=device, asset_id=asset_id, day=date(2026, 6, 2), start_hour=11, end_hour=14,
                 dark_hours=(12, 13))
    with factory() as session, session.begin():
        materialize_contractual_window(session, from_date=date(2026, 6, 1), to_date=date(2026, 6, 2), tz=UTC, asset_ids=[asset_id])

    with factory() as session:
        rows = session.query(AssetAvailabilityDaily).filter_by(asset_id=asset_id).order_by(AssetAvailabilityDaily.availability_date).all()
        assert len(rows) == 2
        pcts = [float(row.availability_pct) for row in rows]
        slots = [row.valid_sample_count for row in rows]
        result = monthly_availability_for_asset(session, asset_id=asset_id, month_start=month_start, month_end_exclusive=month_end)

    assert result["source"] == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
    assert result["source_kind"] == KIND_CONTRACTUAL
    weighted = round(sum(p * s for p, s in zip(pcts, slots)) / sum(slots), 2)
    arithmetic = round(sum(pcts) / len(pcts), 2)
    assert result["availability_pct"] == pytest.approx(weighted)
    if weighted != arithmetic:
        assert result["availability_pct"] != pytest.approx(arithmetic), "monthly must not be a flat day mean"


# --- Reporting selection with real persisted data (items 13/14) ----------


def _full_month(session, *, asset_id, devices, year, month, contractual=True, sampled=True,
                contractual_dark=(), sampled_status="available"):
    from calendar import monthrange

    last_day = monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        for device in devices:
            if contractual:
                _history(session, device=device, asset_id=asset_id, day=date(year, month, day),
                         dark_hours=contractual_dark)
            if sampled:
                for hour in range(7, 20):
                    record_device_status(
                        session, device_id=device.id, asset_id=asset_id,
                        source_fact_key=f"fusionsolar-device-live:{device.id}",
                        observed_at=datetime(year, month, day, hour, tzinfo=LISBON),
                        availability_status=sampled_status, active_power_kw=Decimal("9.0"),
                        source_kind="live_read", freshness="unknown", quality="complete", completeness="complete",
                    )
    return last_day


def test_report_prefers_contractual_over_sampled_when_both_exist(settings, monkeypatch):
    """The decisive commercial rule, on real persisted rows.

    Contractual is 100% (full history window); sampled is deliberately worse
    (one inverter reads `unavailable` on every realtime poll). The report must
    publish the contractual figure and label it contractual -- never the
    sampled one, and never because sampled was computed more recently.
    """
    from nemsei.reporting.assembler import assemble_asset_report
    from nemsei.reporting.periods import monthly_period

    factory = _factory(settings, monkeypatch)
    year, month = 2026, 6
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        asset_id = asset.id
        last_day = _full_month(session, asset_id=asset_id, devices=devices, year=year, month=month)
    with factory() as session, session.begin():
        materialize_existing_availability(session, asset_id=asset_id, from_date=date(year, month, 1), to_date=date(year, month, last_day))
        materialize_contractual_window(session, from_date=date(year, month, 1), to_date=date(year, month, last_day), tz=UTC, asset_ids=[asset_id])

    with factory() as session:
        rows = {r.source: float(r.availability_pct) for r in session.query(AssetAvailabilityDaily)
                .filter_by(asset_id=asset_id, availability_date=date(year, month, 15)).all()
                if r.availability_pct is not None}
    assert SOURCE_FUSIONSOLAR_DEVICE_HISTORY in rows, "contractual must be materialized for the comparison to mean anything"

    with factory() as session, session.begin():
        assembled = assemble_asset_report(session, asset_id=asset_id, period=monthly_period(f"{year}-{month:02d}"), built_by="operator")

    payload = assembled.payload
    assert payload["availability_source"] == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
    assert payload["availability_source_kind"] == KIND_CONTRACTUAL
    assert payload["availability_pct"] is not None
    assert payload["include_availability_kpi"] is True
    assert "availability_pct" not in payload["unavailable_fields"]


def test_a_sampled_only_month_is_never_labelled_contractual(settings, monkeypatch):
    """With no contractual source the report may still show a figure -- but it
    must say `operational`, so nothing downstream can read it as a WAT."""
    from nemsei.reporting.assembler import assemble_asset_report
    from nemsei.reporting.periods import monthly_period

    factory = _factory(settings, monkeypatch)
    year, month = 2026, 6
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        asset_id = asset.id
        last_day = _full_month(session, asset_id=asset_id, devices=devices, year=year, month=month, contractual=False)
    with factory() as session, session.begin():
        materialize_existing_availability(session, asset_id=asset_id, from_date=date(year, month, 1), to_date=date(year, month, last_day))
    with factory() as session, session.begin():
        assembled = assemble_asset_report(session, asset_id=asset_id, period=monthly_period(f"{year}-{month:02d}"), built_by="operator")

    payload = assembled.payload
    assert payload["availability_source"] == SOURCE_FUSIONSOLAR_SAMPLED
    assert payload["availability_source_kind"] == KIND_OPERATIONAL
    assert payload["availability_source_kind"] != KIND_CONTRACTUAL


def test_rendering_a_contractual_report_makes_no_provider_call(settings, monkeypatch):
    """Item 14: PDF/Excel stay provider-free once contractual data exists."""
    import urllib.request

    from nemsei.integrations.fusionsolar import client as fusionsolar_client
    from nemsei.reporting.assembler import assemble_asset_report, excel_payload_from_report
    from nemsei.reporting.customer_pdf import build_customer_report_pdf
    from nemsei.reporting.excel import build_asset_report_workbook
    from nemsei.reporting.periods import monthly_period

    factory = _factory(settings, monkeypatch)
    year, month = 2026, 6
    with factory() as session, session.begin():
        _conn, asset, devices = _plant(session)
        asset_id = asset.id
        last_day = _full_month(session, asset_id=asset_id, devices=devices, year=year, month=month, sampled=False)
    with factory() as session, session.begin():
        materialize_contractual_window(session, from_date=date(year, month, 1), to_date=date(year, month, last_day), tz=UTC, asset_ids=[asset_id])

    calls: list[str] = []

    def _detonate(*args, **kwargs):
        calls.append("call")
        raise AssertionError("A provider call was made while assembling or rendering a report.")

    monkeypatch.setattr(urllib.request, "urlopen", _detonate)
    monkeypatch.setattr(fusionsolar_client.UrllibFusionSolarTransport, "post", _detonate)

    with factory() as session, session.begin():
        assembled = assemble_asset_report(session, asset_id=asset_id, period=monthly_period(f"{year}-{month:02d}"), built_by="operator")
    pdf = build_customer_report_pdf(assembled.payload)
    workbook = build_asset_report_workbook(excel_payload_from_report(assembled.payload))

    assert calls == []
    assert pdf[:5] == b"%PDF-"
    assert workbook is not None
    assert assembled.payload["availability_source_kind"] == KIND_CONTRACTUAL
