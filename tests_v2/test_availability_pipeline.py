"""Pipeline-level guarantees: device history, render isolation, batched materialization.

These cover the three requirements that no single unit can prove --
historical device configuration (yesterday's inverters, not today's), the
rule that rendering a report never reaches a provider, and that the scheduled
materializer scales by query count rather than by asset-days.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event

from nemsei.assets.service import create_asset, create_device, retire_device
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.availability_service import (
    expected_devices_for_date,
    fusionsolar_mapped_asset_ids,
    materialize_availability_window,
)
from nemsei.diagnostics.models import AssetAvailabilityDaily
from nemsei.diagnostics.service import record_device_status
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.rules.availability_window import LISBON
from tests_v2.test_migrations import upgrade

_VALID_FROM = date(2026, 1, 1)


def _factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _connection(session, key):
    return create_connection(
        session, provider_code="fusionsolar", connection_key=key, display_name="Pipeline fixture",
        credential_reference="dev", enabled=True, configuration_status="configured",
    )


# --- Device historical configuration ------------------------------------


def test_expected_devices_follow_the_configuration_of_the_day_not_today(settings, monkeypatch):
    """C was replaced by D mid-month; each half of the month must use its own set.

    The failure this guards against is computing September 1st from
    September 30th's inventory -- which would evaluate D (not yet installed)
    and ignore C (the inverter that actually ran that day).
    """
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session, "conn-history")
        asset = create_asset(session, canonical_name="Swap plant", installed_dc_power_kw=Decimal("90.0"))
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="ST-H", valid_from=_VALID_FROM)

        devices = {}
        for name, valid_from in (("A", _VALID_FROM), ("B", _VALID_FROM), ("C", _VALID_FROM), ("D", date(2026, 9, 16))):
            device = create_device(
                session, asset_id=asset.id, device_kind="inverter", serial_number=f"SN-{name}",
                rated_power_kw=Decimal("30.0"), valid_from=valid_from,
            )
            create_mapping(
                session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"DEV-{name}",
                resource_kind="device", device_id=device.id, valid_from=valid_from,
            )
            devices[name] = device
        # C is replaced by D mid-month, through the supported path -- not by
        # deleting it, which would rewrite the first half of the month.
        retire_device(session, device_id=devices["C"].id, valid_to=date(2026, 9, 15))
        session.commit()

        early = {row["device_id"] for row in expected_devices_for_date(session, asset_id=asset.id, target_date=date(2026, 9, 1))}
        late = {row["device_id"] for row in expected_devices_for_date(session, asset_id=asset.id, target_date=date(2026, 9, 20))}

    assert early == {devices["A"].id, devices["B"].id, devices["C"].id}
    assert late == {devices["A"].id, devices["B"].id, devices["D"].id}
    assert devices["D"].id not in early, "D was not installed on the 1st"
    assert devices["C"].id not in late, "C was gone by the 20th"


def test_a_removed_inverter_keeps_its_history(settings, monkeypatch):
    """Retiring a device must not rewrite what was expected before it left."""
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session, "conn-retire")
        asset = create_asset(session, canonical_name="Retire plant")
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="ST-R", valid_from=_VALID_FROM)
        device = create_device(
            session, asset_id=asset.id, device_kind="inverter", serial_number="SN-R",
            rated_power_kw=Decimal("10.0"), valid_from=_VALID_FROM,
        )
        create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="DEV-R",
            resource_kind="device", device_id=device.id, valid_from=_VALID_FROM,
        )
        retire_device(session, device_id=device.id, valid_to=date(2026, 6, 30))
        # Retiring twice must not churn or change the answer.
        retire_device(session, device_id=device.id, valid_to=date(2026, 6, 30))
        session.commit()
        before = expected_devices_for_date(session, asset_id=asset.id, target_date=date(2026, 6, 1))
        after = expected_devices_for_date(session, asset_id=asset.id, target_date=date(2026, 7, 1))
    assert [row["device_id"] for row in before] == [device.id]
    assert after == []


# --- Batched materialization --------------------------------------------


def test_window_materialization_query_count_does_not_grow_with_asset_days(settings, monkeypatch):
    """Two batched reads for the whole window, not one pair per asset-day.

    The shape this guards: 250 assets x 20 inverters x 30 days must not
    become a query per inverter per day. Reads are counted directly, so the
    guarantee is measured rather than asserted in a comment.
    """
    factory = _factory(settings, monkeypatch)
    engine = build_engine(settings)
    with factory() as session:
        connection = _connection(session, "conn-batch")
        assets = []
        for index in range(4):
            asset = create_asset(session, canonical_name=f"Batch plant {index}", installed_dc_power_kw=Decimal("50.0"))
            create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"ST-B{index}", valid_from=_VALID_FROM)
            device = create_device(
                session, asset_id=asset.id, device_kind="inverter", serial_number=f"SN-B{index}",
                rated_power_kw=Decimal("50.0"), valid_from=_VALID_FROM,
            )
            create_mapping(
                session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"DEV-B{index}",
                resource_kind="device", device_id=device.id, valid_from=_VALID_FROM,
            )
            for day in range(3):
                for hour in (8, 10, 12, 14, 16, 18):
                    record_device_status(
                        session, device_id=device.id, asset_id=asset.id,
                        source_fact_key=f"fusionsolar-device-live:{device.id}",
                        observed_at=datetime(2026, 7, 10 + day, hour, tzinfo=LISBON),
                        availability_status="available", active_power_kw=Decimal("10.0"),
                        day_energy_kwh=Decimal("1.0"), source_kind="live_read",
                        freshness="unknown", quality="complete", completeness="complete",
                    )
            assets.append(asset)
        session.commit()

    selects: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    with factory() as session, session.begin():
        summary = materialize_availability_window(
            session, from_date=date(2026, 7, 10), to_date=date(2026, 7, 12)
        )

    event.remove(engine, "before_cursor_execute", _count)

    assert summary["assets"] == 4
    assert summary["days_recalculated"] == 12  # 4 assets x 3 days
    # Three reads: eligible assets, expected devices, samples. A per-asset-day
    # implementation would issue at least 24 here.
    assert len(selects) <= 6, f"{len(selects)} SELECTs for 12 asset-days: {selects}"


def test_window_materialization_is_idempotent(settings, monkeypatch):
    """Re-running an unchanged window rewrites the same values, no duplicates."""
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        connection = _connection(session, "conn-idem")
        asset = create_asset(session, canonical_name="Idempotent plant", installed_dc_power_kw=Decimal("50.0"))
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="ST-I", valid_from=_VALID_FROM)
        device = create_device(
            session, asset_id=asset.id, device_kind="inverter", serial_number="SN-I",
            rated_power_kw=Decimal("50.0"), valid_from=_VALID_FROM,
        )
        create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="DEV-I",
            resource_kind="device", device_id=device.id, valid_from=_VALID_FROM,
        )
        for hour in range(7, 20):
            record_device_status(
                session, device_id=device.id, asset_id=asset.id,
                source_fact_key=f"fusionsolar-device-live:{device.id}",
                observed_at=datetime(2026, 7, 15, hour, tzinfo=LISBON),
                availability_status="available", active_power_kw=Decimal(str(hour)),
                day_energy_kwh=Decimal("1.0"), source_kind="live_read",
                freshness="unknown", quality="complete", completeness="complete",
            )
        session.commit()

    for _ in range(2):
        with factory() as session, session.begin():
            materialize_availability_window(session, from_date=date(2026, 7, 15), to_date=date(2026, 7, 15), asset_ids=[asset.id])

    with factory() as session:
        rows = session.query(AssetAvailabilityDaily).filter_by(asset_id=asset.id, availability_date=date(2026, 7, 15)).all()
    assert len(rows) == 1


def test_eligible_assets_exclude_those_without_a_fusionsolar_device_mapping(settings, monkeypatch):
    """A Sigenergy asset is not swept into the fleet materialization."""
    factory = _factory(settings, monkeypatch)
    with factory() as session:
        sigen = _connection(session, "conn-sigen-x")
        sigen.provider_code = "sigenergy"
        asset = create_asset(session, canonical_name="Sigenergy plant")
        create_mapping(session, asset_id=asset.id, provider_connection_id=sigen.id, external_id="ST-S", valid_from=_VALID_FROM)
        session.commit()
        assert fusionsolar_mapped_asset_ids(session, on_or_after=date(2026, 7, 1)) == []


# --- Render isolation ----------------------------------------------------


def test_assembling_and_rendering_a_report_never_calls_a_provider(settings, monkeypatch):
    """No FusionSolar or Sigenergy traffic during assemble/PDF/Excel/HTML.

    Enforced at the transport seam rather than by inspecting imports: every
    provider client in this codebase reaches the network through an HTTP
    transport's `post`, and `urllib.request.urlopen` underneath it. Both are
    replaced with detonators, so any call on any path -- including one added
    later by someone who has never read this test -- fails the test loudly
    instead of quietly making a request while a customer PDF is being drawn.
    """
    import urllib.request

    from nemsei.assets.service import create_device
    from nemsei.diagnostics.availability_service import materialize_existing_availability
    from nemsei.integrations.fusionsolar import client as fusionsolar_client
    from nemsei.reporting.assembler import assemble_asset_report, excel_payload_from_report
    from nemsei.reporting.customer_pdf import build_customer_report_pdf
    from nemsei.reporting.excel import build_asset_report_workbook
    from nemsei.reporting.periods import monthly_period

    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session, "conn-render")
        asset = create_asset(session, canonical_name="Render plant", installed_dc_power_kw=Decimal("20.0"))
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="ST-RND", valid_from=_VALID_FROM)
        device = create_device(
            session, asset_id=asset.id, device_kind="inverter", serial_number="SN-RND",
            rated_power_kw=Decimal("20.0"), valid_from=_VALID_FROM,
        )
        create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="DEV-RND",
            resource_kind="device", device_id=device.id, valid_from=_VALID_FROM,
        )
        for day in range(1, 32):
            for hour in range(6, 21):
                record_device_status(
                    session, device_id=device.id, asset_id=asset.id,
                    source_fact_key=f"fusionsolar-device-live:{device.id}",
                    observed_at=datetime(2026, 7, day, hour, tzinfo=LISBON),
                    availability_status="available", active_power_kw=Decimal("5.0"),
                    source_kind="live_read", freshness="unknown", quality="complete", completeness="complete",
                )
        session.flush()
        materialize_existing_availability(session, asset_id=asset.id, from_date=date(2026, 7, 1), to_date=date(2026, 7, 31))
        asset_id = asset.id

    calls: list[str] = []

    def _detonate(*args, **kwargs):
        calls.append(str(args[:1]))
        raise AssertionError("A provider call was made while assembling or rendering a report.")

    monkeypatch.setattr(urllib.request, "urlopen", _detonate)
    monkeypatch.setattr(fusionsolar_client.UrllibFusionSolarTransport, "post", _detonate)

    with factory() as session, session.begin():
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
    pdf_bytes = build_customer_report_pdf(assembled.payload)
    workbook = build_asset_report_workbook(excel_payload_from_report(assembled.payload))

    assert calls == []
    assert pdf_bytes[:5] == b"%PDF-"
    assert workbook is not None
    # And the report really did carry a live availability figure while doing
    # it -- otherwise this would pass trivially on an empty report.
    assert assembled.payload["availability_pct"] == pytest.approx(100.0)
    assert assembled.payload["availability_source"] == "fusionsolar_sampled"
    assert assembled.payload["availability_source_kind"] == "operational"


# --- Retention precondition ---------------------------------------------


def test_unmaterialized_days_are_reported_so_retention_cannot_run_ahead(settings, monkeypatch):
    """Raw facts must never be purged for a day whose availability is unbuilt.

    There is no purge of `device_status_facts` yet; this proves the check a
    future one has to call actually finds the days at risk, and goes quiet
    once they are materialized.
    """
    from nemsei.diagnostics.availability_service import days_with_facts_but_no_availability

    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session, "conn-retain")
        asset = create_asset(session, canonical_name="Retention plant", installed_dc_power_kw=Decimal("10.0"))
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="ST-RT", valid_from=_VALID_FROM)
        device = create_device(
            session, asset_id=asset.id, device_kind="inverter", serial_number="SN-RT",
            rated_power_kw=Decimal("10.0"), valid_from=_VALID_FROM,
        )
        create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="DEV-RT",
            resource_kind="device", device_id=device.id, valid_from=_VALID_FROM,
        )
        for hour in range(8, 19):
            record_device_status(
                session, device_id=device.id, asset_id=asset.id,
                source_fact_key=f"fusionsolar-device-live:{device.id}",
                observed_at=datetime(2026, 7, 20, hour, tzinfo=LISBON),
                availability_status="available", active_power_kw=Decimal("4.0"),
                source_kind="live_read", freshness="unknown", quality="complete", completeness="complete",
            )
        asset_id = asset.id

    with factory() as session:
        pending = days_with_facts_but_no_availability(session, before=date(2026, 8, 1), asset_ids=[asset_id])
    assert pending == [(asset_id, date(2026, 7, 20))], "an unmaterialized day must be visible to retention"

    with factory() as session, session.begin():
        materialize_availability_window(session, from_date=date(2026, 7, 20), to_date=date(2026, 7, 20), asset_ids=[asset_id])

    with factory() as session:
        assert days_with_facts_but_no_availability(session, before=date(2026, 8, 1), asset_ids=[asset_id]) == []
