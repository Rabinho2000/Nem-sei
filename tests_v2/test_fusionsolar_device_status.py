from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone as datetime_timezone

from sqlalchemy import select

from nemsei.assets.service import create_asset, create_device
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.models import DeviceStatusFact
from nemsei.integrations.fusionsolar.client import FusionSolarClient, HttpResponse
from nemsei.integrations.fusionsolar.device_status import (
    FusionSolarDeviceStatusService,
    device_contract_for,
    normalize_device_realtime_row,
    normalize_device_type_row,
)
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sync.models import SyncRun
from tests_v2.test_migrations import upgrade


LOGIN_OK = {"success": True, "failCode": 0, "data": None}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, payload, headers, timeout_seconds):
        self.calls.append((url, payload, headers))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, payload)


def devlist(rows):
    return response({"success": True, "failCode": 0, "data": {"list": rows}})


def devkpi(rows):
    return response({"success": True, "failCode": 0, "data": rows})


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def configured_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_BASE_URL", "https://fusion.example.test")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_POWER_UNIT", "kW")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_ENERGY_UNIT", "kWh")


def selected_connection(factory, *, count=1, station_code="FS-STATION"):
    with factory() as session:
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="fusion-device-status",
            display_name="Fusion device status",
            credential_reference="dev",
            enabled=True,
            configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Device status asset")
        create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id=station_code)
        devices = []
        for number in range(1, count + 1):
            device = create_device(session, asset_id=asset.id, device_kind="inverter", serial_number=f"SN-{number}")
            mapping = create_mapping(
                session,
                asset_id=asset.id,
                provider_connection_id=connection.id,
                external_id=f"DEV-{number:03d}",
                resource_kind="device",
                device_id=device.id,
            )
            devices.append((device, mapping))
        session.commit()
        return connection.id, asset.id, station_code, devices


def service(factory, settings, transport):
    configured = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    return FusionSolarDeviceStatusService(
        factory,
        configured,
        client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport),
    )


def devrow(dev_dn, dev_type_id):
    return {"devDn": dev_dn, "devTypeId": dev_type_id}


def kpirow(dev_dn, *, state, power, energy, collect_time=None):
    data = {"inverter_state": state, "active_power": power, "day_cap": energy}
    row = {"devDn": dev_dn, "dataItemMap": data}
    if collect_time is not None:
        row["collectTime"] = collect_time
    return row


def test_device_contract_requires_explicit_verified_units(monkeypatch):
    from nemsei.integrations.fusionsolar.client import FusionSolarClientError
    from nemsei.providers.models import ProviderConnection

    connection = ProviderConnection(provider_code="fusionsolar", connection_key="k", display_name="k", credential_reference="dev")
    try:
        device_contract_for(connection)
        assert False, "must require configuration"
    except FusionSolarClientError as exc:
        assert exc.error.code.value == "configuration"
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_POWER_UNIT", "MW")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_ENERGY_UNIT", "kWh")
    try:
        device_contract_for(connection)
        assert False, "must reject an unverified unit"
    except FusionSolarClientError:
        pass
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_POWER_UNIT", "kW")
    contract = device_contract_for(connection)
    assert contract.active_power_unit == "kW" and contract.day_energy_unit == "kWh"


def test_device_type_row_only_recognizes_typed_identifiable_devices():
    assert normalize_device_type_row(devrow("DEV-001", 1)).device_type_id == 1
    assert normalize_device_type_row({"devDn": "DEV-001"}) is None
    assert normalize_device_type_row({"devTypeId": 1}) is None


def test_realtime_row_never_reads_mppt_string_fields_and_derives_freshness_honestly():
    from nemsei.integrations.fusionsolar.device_status import FusionSolarDeviceContract

    contract = FusionSolarDeviceContract(active_power_unit="kW", day_energy_unit="kWh")
    ingested = datetime(2026, 7, 1, 12, 0, tzinfo=datetime_timezone.utc)
    row = {
        "devDn": "DEV-001",
        "dataItemMap": {
            "inverter_state": "512", "active_power": "3.5", "day_cap": "12.4",
            "pv1_i": "8.1", "pv1_u": "620",  # must never surface in the sample
        },
    }
    sample = normalize_device_realtime_row(row, expected_external_ids=frozenset({"DEV-001"}), contract=contract, ingested_at=ingested)
    assert sample.availability_status == "available"
    assert sample.active_power_kw == 3.5 or float(sample.active_power_kw) == 3.5
    assert not hasattr(sample, "pv1_i") and not hasattr(sample, "mppt")
    # No collectTime: freshness is "unknown", never defaulted to "fresh" -- the
    # deliberate divergence from V1's own `has_recent_data` default.
    assert sample.freshness == "unknown"
    assert sample.observed_at == ingested

    fresh_row = dict(row, collectTime=int(ingested.timestamp() * 1000))
    fresh_sample = normalize_device_realtime_row(fresh_row, expected_external_ids=frozenset({"DEV-001"}), contract=contract, ingested_at=ingested)
    assert fresh_sample.freshness == "fresh"

    stale_ms = int((ingested - timedelta(hours=6)).timestamp() * 1000)
    stale_row = dict(row, collectTime=stale_ms)
    stale_sample = normalize_device_realtime_row(stale_row, expected_external_ids=frozenset({"DEV-001"}), contract=contract, ingested_at=ingested)
    assert stale_sample.freshness == "stale"


def test_watt_unit_is_scaled_by_verified_contract_not_by_magnitude_guess():
    from nemsei.integrations.fusionsolar.device_status import FusionSolarDeviceContract

    contract = FusionSolarDeviceContract(active_power_unit="W", day_energy_unit="kWh")
    row = {"devDn": "DEV-001", "dataItemMap": {"inverter_state": "512", "active_power": "3500", "day_cap": "12.4"}}
    sample = normalize_device_realtime_row(row, expected_external_ids=frozenset({"DEV-001"}), contract=contract, ingested_at=datetime.now(datetime_timezone.utc))
    assert float(sample.active_power_kw) == 3.5


def test_sync_persists_only_verified_inverter_types_with_provenance(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, asset_id, station_code, devices = selected_connection(factory, count=2, station_code="FS-STATION")
    device_1, mapping_1 = devices[0]
    device_2, mapping_2 = devices[1]
    transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "token"}),
        devlist([devrow("DEV-001", 1), devrow("DEV-002", 38), devrow("DEV-OTHER", 17)]),
        devkpi([kpirow("DEV-001", state="512", power="3.5", energy="12.4")]),
        devkpi([kpirow("DEV-002", state="768", power="0", energy="4.0")]),
    ])
    result = service(factory, settings, transport).sync_device_status(connection_id)
    assert result.status == "success"
    assert result.accepted == 2
    assert transport.calls[1][0].endswith("/thirdData/getDevList")
    assert transport.calls[1][1] == {"stationCodes": station_code}
    with factory() as session:
        facts = list(session.scalars(select(DeviceStatusFact).order_by(DeviceStatusFact.device_id)))
        assert len(facts) == 2
        assert {fact.device_id for fact in facts} == {device_1.id, device_2.id}
        assert all(fact.source_kind == "live_read" for fact in facts)
        assert all(fact.freshness == "unknown" for fact in facts)  # no collectTime in fixture
        assert all(fact.sync_run_id == result.sync_run_id for fact in facts)
        by_device = {fact.device_id: fact for fact in facts}
        assert by_device[device_1.id].availability_status == "available"
        assert by_device[device_2.id].availability_status == "unavailable"
        run = session.get(SyncRun, result.sync_run_id)
        assert run.metadata_json["actual_provider_calls"] == 4


def test_repeat_is_idempotent_and_changed_reading_creates_a_revision(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, asset_id, station_code, devices = selected_connection(factory, count=1)
    device_1, _mapping = devices[0]

    def run_once(state):
        transport = FakeTransport([
            response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
            devlist([devrow("DEV-001", 1)]),
            devkpi([kpirow("DEV-001", state=state, power="3.5", energy="12.4")]),
        ])
        return service(factory, settings, transport).sync_device_status(connection_id)

    first = run_once("512")
    second = run_once("512")
    corrected = run_once("768")
    assert {first.status, second.status, corrected.status} == {"success"}
    with factory() as session:
        facts = list(session.scalars(select(DeviceStatusFact).where(DeviceStatusFact.device_id == device_1.id).order_by(DeviceStatusFact.source_revision)))
        assert [(fact.source_revision, fact.availability_status) for fact in facts] == [(1, "available"), (2, "unavailable")]
        assert facts[1].supersedes_fact_id == facts[0].id


def test_unverified_device_types_are_not_persisted(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, asset_id, station_code, devices = selected_connection(factory, count=1)
    transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        devlist([devrow("DEV-001", 17)]),  # not an inverter dev_type_id
    ])
    result = service(factory, settings, transport).sync_device_status(connection_id)
    assert result.status == "partial"
    assert result.accepted == 0
    with factory() as session:
        assert session.scalar(select(DeviceStatusFact)) is None


def test_missing_device_contract_configuration_makes_no_provider_call(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_USERNAME", "u")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_PASSWORD", "p")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_BASE_URL", "https://fusion.example.test")
    # Deliberately no NEMSEI_V2_FUSIONSOLAR_DEV_DEVICE_POWER_UNIT/_ENERGY_UNIT.
    factory = factory_for(settings, monkeypatch)
    connection_id, *_rest = selected_connection(factory, count=1)
    transport = FakeTransport([])
    result = service(factory, settings, transport).sync_device_status(connection_id)
    assert result.status == "failed" and result.error_code == "configuration"
    assert not transport.calls
