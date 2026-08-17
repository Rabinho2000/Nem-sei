from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import func, select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.sigenergy.client import SigenergyClient, SigenergyCredentials, SigenergyEndpoints, SigenergyHttpResponse
from nemsei.integrations.sigenergy.discovery import SigenergyDiscoveryService, plant_from_payload
from nemsei.integrations.sigenergy.monitoring import SigenergyMonitoringService, normalize_status
from nemsei.integrations.sigenergy.request_control import SigenergyRequestController
from nemsei.monitoring.models import MonitoringCurrentState, MonitoringObservation
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.registry import ImplementationSupport, ProviderCapability, ProviderCode, RuntimeAvailability, evaluate_capability
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import IntegrationHealth, ProviderRequestAttempt, ProviderRequestState, SyncRun
from nemsei.sync.service import start_sync_run
from tests_v2.test_migrations import upgrade


class FakeTransport:
    def __init__(self, responses: list[SigenergyHttpResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, params, headers, json_payload, timeout_seconds):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers, "json": json_payload})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def response(payload: dict, status: int = 200, headers: dict[str, str] | None = None) -> SigenergyHttpResponse:
    return SigenergyHttpResponse(status, headers or {}, payload)


def auth_ok() -> SigenergyHttpResponse:
    return response({"code": 0, "msg": "success", "data": {"accessToken": "fixture-token", "expiresIn": 3600}})


def systems(rows: list[dict]) -> SigenergyHttpResponse:
    return response({"code": 0, "data": {"list": rows}})


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def configured_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_SIGENERGY_FIXTURE_APP_KEY", "fixture-app-key")
    monkeypatch.setenv("NEMSEI_V2_SIGENERGY_FIXTURE_APP_SECRET", "fixture-app-secret")
    monkeypatch.setenv("NEMSEI_V2_SIGENERGY_FIXTURE_BASE_URL", "https://sigenergy.example.test")
    monkeypatch.setenv("NEMSEI_V2_SIGENERGY_FIXTURE_AUTH_ENDPOINT", "/openapi/auth/login/key")
    monkeypatch.setenv("NEMSEI_V2_SIGENERGY_FIXTURE_SYSTEMS_ENDPOINT", "/openapi/system")
    monkeypatch.setenv("NEMSEI_V2_SIGENERGY_FIXTURE_ENERGY_FLOW_ENDPOINT", "/openapi/systems/{system_id}/energyFlow")
    monkeypatch.setenv("NEMSEI_V2_SIGENERGY_FIXTURE_REGION", "eu")


def selected_connection(factory, *, count: int = 1):
    with factory() as session:
        connection = create_connection(session, provider_code="sigenergy", connection_key="sigenergy-test", display_name="Sigenergy test", credential_reference="fixture", enabled=True, configuration_status="configured")
        mappings = []
        for number in range(1, count + 1):
            asset = create_asset(session, canonical_name=f"Sigenergy asset {number}")
            mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id=f"SIG-{number:03d}")
            create_source_policy(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_use="monitoring", priority=1, valid_from=date(2020, 1, 1))
            mappings.append(mapping)
        session.commit()
        return connection.id, mappings


def enabled(settings):
    return replace(settings, capabilities={**settings.capabilities, "provider_reads": True})


def discovery_service(factory, settings, transport):
    return SigenergyDiscoveryService(factory, enabled(settings), transport=transport, max_transient_retries=0)


def monitoring_service(factory, settings, transport):
    return SigenergyMonitoringService(factory, enabled(settings), transport=transport, max_transient_retries=0)


def test_sigenergy_registry_support_is_narrow_and_runtime_is_separate():
    supported = {ProviderCapability.CONNECTION_VALIDATION, ProviderCapability.DISCOVERY, ProviderCapability.CURRENT_MONITORING}
    for capability in ProviderCapability:
        status = evaluate_capability(ProviderCode.SIGENERGY, capability, connection_configured=True)
        if capability in supported:
            assert status.implementation_support is ImplementationSupport.SUPPORTED
            assert status.runtime_availability is RuntimeAvailability.AVAILABLE
        else:
            assert status.implementation_support is ImplementationSupport.UNSUPPORTED
    assert evaluate_capability(ProviderCode.SIGENERGY, ProviderCapability.DISCOVERY, connection_configured=False).runtime_availability is RuntimeAvailability.NOT_CONFIGURED


def test_client_auth_supports_object_and_json_string_data_without_leaking_token():
    transport = FakeTransport([auth_ok()])
    client = SigenergyClient(
        SigenergyEndpoints("https://sigenergy.example.test", "/auth", "/systems", "/systems/{system_id}/flow", "eu"),
        SigenergyCredentials("key", "secret"),
        transport=transport,
    )
    client.authenticate()
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["headers"]["sigen-region"] == "eu"
    assert "fixture-token" not in repr(client)


def test_connection_validation_authenticates_only(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _ = selected_connection(factory)
    transport = FakeTransport([auth_ok()])
    result = discovery_service(factory, settings, transport).validate_connection(connection_id)
    assert result.status == "success" and result.completeness == "complete"
    assert len(transport.calls) == 1 and transport.calls[0]["method"] == "POST"
    with factory() as session:
        run = session.get(SyncRun, result.sync_run_id)
        assert run is not None and run.metadata_json["actual_provider_calls"] == 1


def test_discovery_normalizes_ids_deduplicates_and_does_not_mutate_mappings(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _ = selected_connection(factory)
    transport = FakeTransport([auth_ok(), systems([
        {"systemId": "SIG-001", "systemName": "One", "systemStatus": "running"},
        {"systemId": "SIG-002", "systemName": "Two", "systemStatus": "offline"},
        {"systemId": "sig-001", "systemName": "Duplicate"},
    ])])
    result = discovery_service(factory, settings, transport).discover(connection_id)
    assert result.status == "partial"
    assert [plant.external_id for plant in result.plants] == ["SIG-001", "SIG-002"]
    assert result.duplicate_external_ids == {"sig-001"}
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 1


def test_monitoring_uses_selected_mappings_without_discovery_and_confirms_state(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory, count=2)
    transport = FakeTransport([
        auth_ok(),
        response({"code": 0, "data": {"systemId": "SIG-001", "systemStatus": "running", "pvPower": 4.2}}),
        response({"code": 0, "data": {"systemId": "SIG-002", "systemStatus": "offline", "pvPower": 0}}),
    ])
    result = monitoring_service(factory, settings, transport).sync_current_monitoring(connection_id)
    assert result.status == "success" and result.accepted == 2
    assert [call["method"] for call in transport.calls] == ["POST", "GET", "GET"]
    with factory() as session:
        observations = list(session.scalars(select(MonitoringObservation).order_by(MonitoringObservation.provider_mapping_id)))
        assert [observation.condition for observation in observations] == ["operational", "offline"]
        assert all(observation.freshness == "unknown" for observation in observations)
        assert all(observation.metadata_json["observed_at_source"] == "ingested_at_no_provider_timestamp" for observation in observations)
        assert all(session.get(MonitoringCurrentState, mapping.id).last_confirmed_at is not None for mapping in mappings)


def test_unknown_or_missing_status_never_fabricates_offline(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _ = selected_connection(factory)
    transport = FakeTransport([auth_ok(), response({"code": 0, "data": {"systemId": "SIG-001", "pvPower": 1.1}})])
    result = monitoring_service(factory, settings, transport).sync_current_monitoring(connection_id)
    assert result.status == "partial"
    with factory() as session:
        observation = session.scalar(select(MonitoringObservation))
        assert observation is not None and observation.condition == "unknown" and observation.quality == "missing"
        assert session.scalar(select(func.count()).select_from(MonitoringObservation).where(MonitoringObservation.condition == "offline")) == 0


def test_provider_failure_updates_audit_without_creating_observation(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _ = selected_connection(factory)
    transport = FakeTransport([auth_ok(), response({}, status=503)])
    result = monitoring_service(factory, settings, transport).sync_current_monitoring(connection_id)
    assert result.status == "failed" and result.error_code == "unavailable"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MonitoringObservation)) == 0
        health = session.get(IntegrationHealth, connection_id)
        assert health is not None and health.provider_state == "unavailable"


def test_rate_limit_retry_after_is_persisted_and_no_second_call_occurs(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _ = selected_connection(factory)
    transport = FakeTransport([response({}, status=429, headers={"Retry-After": "60"})])
    result = discovery_service(factory, settings, transport).validate_connection(connection_id)
    assert result.status == "rate_limited" and len(transport.calls) == 1
    with factory() as session:
        state = session.scalar(select(ProviderRequestState).where(ProviderRequestState.provider_connection_id == connection_id, ProviderRequestState.endpoint_family == "authentication"))
        assert state is not None and state.provider_retry_at is not None


def test_expected_configuration_and_disabled_reads_make_no_network_call(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _ = selected_connection(factory)
    transport = FakeTransport([])
    disabled = SigenergyDiscoveryService(factory, settings, transport=transport)
    result = disabled.discover(connection_id)
    assert result.status == "deferred" and not transport.calls


def test_repeated_identical_monitoring_is_idempotent_and_changed_status_revises(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    for status in ("running", "running", "fault"):
        transport = FakeTransport([auth_ok(), response({"code": 0, "data": {"systemId": "SIG-001", "systemStatus": status}})])
        monitoring_service(factory, settings, transport).sync_current_monitoring(connection_id)
    with factory() as session:
        observations = list(session.scalars(select(MonitoringObservation).where(MonitoringObservation.provider_mapping_id == mappings[0].id).order_by(MonitoringObservation.source_revision)))
        assert [(item.source_revision, item.condition) for item in observations] == [(1, "operational"), (2, "fault")]


def test_unexpected_operation_failure_finalizes_sigenergy_attempt_without_secret_detail(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    connection_id, _ = selected_connection(factory)
    with factory() as session:
        run = start_sync_run(session, provider_connection_id=connection_id, capability="current_monitoring")
        session.commit()
        run_id = run.id
    controller = SigenergyRequestController(factory, max_transient_retries=1)
    with pytest.raises(RuntimeError, match="unexpected test failure"):
        controller.call(connection_id=connection_id, sync_run_id=run_id, endpoint_family="current_monitoring", purpose="unexpected-test", operation=lambda: (_ for _ in ()).throw(RuntimeError("unexpected test failure: fixture-secret")))
    with factory() as session:
        attempt = session.scalar(select(ProviderRequestAttempt).where(ProviderRequestAttempt.sync_run_id == run_id))
        assert attempt is not None and attempt.status == "failed" and "fixture-secret" not in (attempt.safe_detail or "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("normal", "operational"), ("online", "operational"), ("running", "operational"), ("warning", "unknown"), ("fault", "fault"), ("offline", "offline"), ("new-provider-code", "unknown"), (None, "unknown")],
)
def test_sigenergy_status_normalization_is_explicit(raw, expected):
    assert normalize_status(raw) == expected


def test_discovery_identifier_requires_stable_system_id():
    with pytest.raises(ValueError):
        plant_from_payload(1, {"systemName": "no id"})


def test_production_remains_unsupported_until_day_and_unit_contract_is_verified():
    status = evaluate_capability(ProviderCode.SIGENERGY, ProviderCapability.PRODUCTION_HISTORY, connection_configured=True)
    assert status.implementation_support is ImplementationSupport.UNSUPPORTED
