from __future__ import annotations

from dataclasses import replace
from datetime import date

from sqlalchemy import delete, func, select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, HttpResponse
from nemsei.integrations.fusionsolar.monitoring import FusionSolarMonitoringService, normalize_current_monitoring_row
from nemsei.monitoring.models import MonitoringObservation
from nemsei.monitoring.service import record_observation
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.sources.service import create_source_policy
from nemsei.sources.models import AssetSourcePolicy
from nemsei.sync.models import IntegrationHealth, ProviderRequestState, SyncRun
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


def realtime(rows):
    return response({"success": True, "failCode": 0, "data": rows})


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def configured_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_MONITOR_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_MONITOR_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_MONITOR_BASE_URL", "https://fusion.example.test")


def selected_connection(factory, *, count=1):
    with factory() as session:
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="fusion-monitoring",
            display_name="Fusion monitoring",
            credential_reference="monitor",
            enabled=True,
            configuration_status="configured",
        )
        mappings = []
        for number in range(1, count + 1):
            asset = create_asset(session, canonical_name=f"Monitoring asset {number}")
            mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"FS-{number:03d}")
            create_source_policy(
                session,
                asset_id=asset.id,
                provider_mapping_id=mapping.id,
                source_use="monitoring",
                priority=1,
                valid_from=date(2020, 1, 1),
            )
            mappings.append(mapping)
        session.commit()
        return connection.id, mappings


def service(factory, settings, transport, *, retries=0):
    configured = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    return FusionSolarMonitoringService(
        factory,
        configured,
        client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport),
        max_transient_retries=retries,
    )


def row(code, health):
    return {"stationCode": code, "dataItemMap": {"real_health_state": health}}


def test_current_monitoring_normalizes_only_verified_codes_and_keeps_freshness_separate():
    assert normalize_current_monitoring_row(row("FS-001", "3")).condition == "operational"
    assert normalize_current_monitoring_row(row("FS-001", "2")).condition == "fault"
    assert normalize_current_monitoring_row(row("FS-001", "1")).condition == "offline"
    unknown = normalize_current_monitoring_row(row("FS-001", "warning"))
    assert unknown.condition == "unknown" and unknown.quality == "unknown"
    missing = normalize_current_monitoring_row({"stationCode": "FS-001", "dataItemMap": {}})
    assert missing.condition == "unknown" and missing.quality == "missing"


def test_monitoring_batches_once_without_discovery_and_persists_canonical_observations(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory, count=3)
    transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "token"}),
        realtime([row("FS-001", "3"), row("FS-002", "2"), row("FS-003", "1")]),
    ])
    result = service(factory, settings, transport).sync_current_monitoring(connection_id)
    assert result.status == "success" and result.accepted == 3
    assert len(transport.calls) == 2
    assert transport.calls[1][0].endswith("/thirdData/getStationRealKpi")
    assert transport.calls[1][1] == {"stationCodes": "FS-001,FS-002,FS-003"}
    with factory() as session:
        observations = list(session.scalars(select(MonitoringObservation).order_by(MonitoringObservation.provider_mapping_id)))
        run = session.get(SyncRun, result.sync_run_id)
        assert [item.condition for item in observations] == ["operational", "fault", "offline"]
        assert all(item.freshness == "unknown" for item in observations)
        assert all(item.metadata_json == {"observed_at_source": "ingested_at_no_provider_timestamp"} for item in observations)
        assert all(item.provider_mapping_id in {mapping.id for mapping in mappings} for item in observations)
        assert run.metadata_json["actual_provider_calls"] == 2


def test_unknown_and_missing_states_are_persisted_without_fake_operational_state(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory, count=2)
    transport = FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "99"), {"stationCode": "FS-002", "dataItemMap": {}}])])
    result = service(factory, settings, transport).sync_current_monitoring(connection_id)
    assert result.status == "success"
    with factory() as session:
        values = list(session.scalars(select(MonitoringObservation).order_by(MonitoringObservation.provider_mapping_id)))
        assert [(value.condition, value.quality) for value in values] == [("unknown", "unknown"), ("unknown", "missing")]


def test_repeat_is_idempotent_and_changed_provider_evidence_creates_a_revision(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    first = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "3")])])).sync_current_monitoring(connection_id)
    second = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "3")])])).sync_current_monitoring(connection_id)
    corrected = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "2")])])).sync_current_monitoring(connection_id)
    assert {first.status, second.status, corrected.status} == {"success"}
    with factory() as session:
        values = list(session.scalars(select(MonitoringObservation).where(MonitoringObservation.provider_mapping_id == mappings[0].id).order_by(MonitoringObservation.source_revision)))
        assert [(value.source_revision, value.condition) for value in values] == [(1, "operational"), (2, "fault")]
        assert values[1].supersedes_observation_id == values[0].id


def test_partial_response_and_provider_failure_preserve_successful_or_stale_observations(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory, count=2)
    partial = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "3")])])).sync_current_monitoring(connection_id)
    assert partial.status == "partial" and partial.accepted == 1
    with factory() as session:
        record_observation(
            session,
            asset_id=mappings[1].asset_id,
            provider_mapping_id=mappings[1].id,
            source_observation_key="previous-stale",
            observed_at=utc_now(),
            condition="operational",
            freshness="stale",
            quality="partial",
            completeness="partial",
        )
        session.commit()
    failed = service(factory, settings, FakeTransport([FusionSolarClientError(ProviderError(ProviderErrorCode.TIMEOUT, "timeout", transient=True))])).sync_current_monitoring(connection_id)
    assert failed.status == "failed"
    with factory() as session:
        stale = session.scalar(select(MonitoringObservation).where(MonitoringObservation.source_observation_key == "previous-stale"))
        assert stale is not None and stale.condition == "operational" and stale.freshness == "stale"
        assert session.scalar(select(func.count()).select_from(MonitoringObservation).where(MonitoringObservation.condition == "offline")) == 0


def test_rate_limit_deferral_and_source_policy_failure_make_no_monitoring_call(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    limited = service(factory, settings, FakeTransport([FusionSolarClientError(ProviderError(ProviderErrorCode.RATE_LIMITED, "later", retry_after_seconds=60, transient=True))])).sync_current_monitoring(connection_id)
    assert limited.status == "rate_limited"
    with factory() as session:
        state = session.scalar(select(ProviderRequestState).where(ProviderRequestState.provider_connection_id == connection_id, ProviderRequestState.endpoint_family == "authentication"))
        assert state is not None and state.provider_retry_at is not None
    blocked = service(factory, settings, FakeTransport([])).sync_current_monitoring(connection_id)
    assert blocked.status == "rate_limited"
    with factory() as session:
        health = session.get(IntegrationHealth, connection_id)
        assert health.provider_state == "healthy" and health.last_error_code == "rate_limited"


def test_missing_source_policy_is_a_configuration_finding_without_provider_calls(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    with factory() as session:
        session.execute(delete(AssetSourcePolicy))
        session.commit()
    transport = FakeTransport([])
    result = service(factory, settings, transport).sync_current_monitoring(connection_id)
    assert result.status == "failed" and result.error_code == "configuration" and not transport.calls
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MonitoringObservation).where(MonitoringObservation.provider_mapping_id == mappings[0].id)) == 0
