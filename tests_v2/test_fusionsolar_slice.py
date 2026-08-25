from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from sqlalchemy import func, select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, HttpResponse
from nemsei.integrations.fusionsolar.discovery import MappingValidationStatus, ReconciliationStatus
from nemsei.integrations.fusionsolar.service import FusionSolarDiscoveryService
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.registry import ImplementationSupport, ProviderCapability, ProviderCode, RuntimeAvailability, evaluate_capability
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.sync.models import IntegrationHealth, ProviderRequestAttempt, ProviderRequestState, SyncRun
from nemsei.sync.service import reserve_request
from tests_v2.test_migrations import upgrade


LOGIN_OK = {"success": True, "failCode": 0, "message": "OK", "data": None}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, payload, headers, timeout_seconds):
        self.calls.append((url, payload, headers))
        next_response = self.responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, payload)


def page(rows, *, page_no=1, page_count=1):
    return response({"success": True, "failCode": 0, "data": {"pageNo": page_no, "pageCount": page_count, "list": rows}})


def session_factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def connection_and_mapping(factory):
    with factory() as session:
        asset = create_asset(session, canonical_name="FusionSolar target")
        connection = create_connection(session, provider_code="fusionsolar", connection_key="fusion-slice", display_name="Fusion slice", credential_reference="fixture", enabled=True, configuration_status="configured")
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="FS-001")
        session.commit()
        return connection.id, mapping.id


def service(factory, settings, transport, *, retries=0):
    enabled = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    return FusionSolarDiscoveryService(factory, enabled, client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport), max_transient_retries=retries)


def configured_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_FIXTURE_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_FIXTURE_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_FIXTURE_BASE_URL", "https://fusion.example.test")


def test_live_implemented_capabilities_are_narrow_and_runtime_availability_is_separate():
    for capability in ProviderCapability:
        status = evaluate_capability(ProviderCode.FUSIONSOLAR, capability, connection_configured=True)
        if capability in {
            ProviderCapability.CONNECTION_VALIDATION,
            ProviderCapability.DISCOVERY,
            ProviderCapability.CURRENT_MONITORING,
            ProviderCapability.PRODUCTION_HISTORY,
            # M7 Fatia 2 (docs/v2/DEVICE_TELEMETRY.md): getDevList + getDevRealKpi,
            # both V1-evidenced, behind their own verified-contract gate.
            ProviderCapability.DEVICE_DISCOVERY,
            ProviderCapability.DEVICE_MONITORING,
        }:
            assert status.implementation_support is ImplementationSupport.SUPPORTED
            assert status.runtime_availability is RuntimeAvailability.AVAILABLE
        else:
            assert status.implementation_support is ImplementationSupport.UNSUPPORTED
    # The cross-provider half of this test: the two providers are not the same
    # shape, and this is where that stays visible.
    for capability in {
        ProviderCapability.CONNECTION_VALIDATION,
        ProviderCapability.DISCOVERY,
        ProviderCapability.CURRENT_MONITORING,
        # Sigenergy gained production history on 2026-08-25, after its day and
        # unit contract was verified against the live API.
        ProviderCapability.PRODUCTION_HISTORY,
    }:
        assert evaluate_capability(ProviderCode.SIGENERGY, capability, connection_configured=True).implementation_support is ImplementationSupport.SUPPORTED
    # Device level stays the real difference: V1 never called a Sigenergy
    # device endpoint at all, so there is nothing to audit a contract against.
    for capability in {ProviderCapability.DEVICE_DISCOVERY, ProviderCapability.DEVICE_MONITORING}:
        assert evaluate_capability(ProviderCode.SIGENERGY, capability, connection_configured=True).implementation_support is ImplementationSupport.UNSUPPORTED


def test_discovery_paginates_deduplicates_and_reconciles_without_creating_assets(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = session_factory(settings, monkeypatch)
    connection_id, mapping_id = connection_and_mapping(factory)
    transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "test-token"}),
        page([{"plantCode": "FS-001", "plantName": "Mapped name"}, {"plantCode": "FS-002", "plantName": "Unmapped"}], page_count=2),
        page([{"plantCode": "FS-002", "plantName": "Duplicate"}, {"stationCode": "FS-003", "stationName": "Third"}], page_no=2, page_count=2),
    ])
    result = service(factory, settings, transport).discover(connection_id)
    assert result.status == "success" and result.completeness == "complete"
    assert [plant.external_id for plant in result.plants] == ["FS-001", "FS-002", "FS-003"]
    assert result.duplicate_external_ids == {"FS-002"}
    assert len(transport.calls) == 3
    assert transport.calls[1][2]["XSRF-TOKEN"] == "test-token"
    reconciliation = service(factory, settings, transport).reconcile(result)
    assert [item.status for item in reconciliation] == [ReconciliationStatus.MAPPED, ReconciliationStatus.DUPLICATE_CONFLICT, ReconciliationStatus.UNMAPPED]
    validation = service(factory, settings, transport).validate_mapping(mapping_id, discovery=result)
    assert validation.status is MappingValidationStatus.VALID
    assert len(transport.calls) == 3  # passed discovery result prevents a second provider call
    with factory() as session:
        run = session.get(SyncRun, result.sync_run_id)
        assert run.metadata_json == {"actual_provider_calls": 3, "items_received": 3, "items_accepted": 3, "items_rejected": 0, "duplicate_ids": 1, "pages_completed": 2}
        assert session.scalar(select(func.count()).select_from(AssetProviderMapping)) == 1


def test_connection_validation_reads_one_page_and_reports_access_without_a_full_discovery(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = session_factory(settings, monkeypatch)
    connection_id, _mapping_id = connection_and_mapping(factory)
    transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "test-token"}),
        page([{"plantCode": "FS-001"}], page_count=3),
    ])
    result = service(factory, settings, transport).validate_connection(connection_id)
    assert result.status == "success" and result.completeness == "partial"
    assert len(transport.calls) == 2


def test_auth_access_timeout_and_rate_limit_are_normalized_and_safe(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = session_factory(settings, monkeypatch)
    connection_id, mapping_id = connection_and_mapping(factory)
    bad_auth = FakeTransport([response({"success": False, "failCode": 201, "message": "no"})])
    result = service(factory, settings, bad_auth).discover(connection_id)
    assert result.status == "failed" and result.error_code == "authentication"
    with factory() as session:
        assert session.get(IntegrationHealth, connection_id).auth_state == "degraded"
    denied = FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), response({}, status=403)])
    result = service(factory, settings, denied).discover(connection_id)
    assert service(factory, settings, denied).validate_mapping(mapping_id, discovery=result).status is MappingValidationStatus.ACCESS_DENIED
    timeout = FakeTransport([FusionSolarClientError(ProviderError(ProviderErrorCode.TIMEOUT, "timeout", transient=True))])
    result = service(factory, settings, timeout).discover(connection_id)
    assert result.status == "failed"
    assert service(factory, settings, timeout).validate_mapping(mapping_id, discovery=result).status is MappingValidationStatus.PROVIDER_UNAVAILABLE
    limited = FakeTransport([FusionSolarClientError(ProviderError(ProviderErrorCode.RATE_LIMITED, "later", retry_after_seconds=60, transient=True))])
    result = service(factory, settings, limited).discover(connection_id)
    assert result.status == "rate_limited"
    with factory() as session:
        state = session.scalar(select(ProviderRequestState).where(ProviderRequestState.provider_connection_id == connection_id, ProviderRequestState.endpoint_family == "authentication"))
        assert state is not None and state.quota_known is False and state.provider_retry_at is not None
        assert session.get(AssetProviderMapping, mapping_id).mapping_status == "active"


def test_partial_discovery_deferred_control_and_configuration_do_not_call_network(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = session_factory(settings, monkeypatch)
    connection_id, _mapping_id = connection_and_mapping(factory)
    partial_transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        page([{"plantCode": "FS-001"}], page_count=2),
        FusionSolarClientError(ProviderError(ProviderErrorCode.UNAVAILABLE, "outage", transient=True)),
    ])
    result = service(factory, settings, partial_transport).discover(connection_id)
    assert result.status == "partial" and result.completeness == "partial"
    with factory() as session:
        state, _attempt, allowed = reserve_request(session, provider_connection_id=connection_id, endpoint_family="authentication", purpose="block", now=utc_now())
        state.next_allowed_at = utc_now() + timedelta(minutes=5)
        session.commit()
    blocked = FakeTransport([])
    result = service(factory, settings, blocked).discover(connection_id)
    assert result.status == "rate_limited" and not blocked.calls
    disabled = FusionSolarDiscoveryService(factory, settings, client_factory=lambda credentials: FusionSolarClient(credentials, transport=FakeTransport([])))
    result = disabled.discover(connection_id)
    assert result.status == "deferred"


def test_missing_secret_configuration_is_recorded_without_a_network_call(settings, monkeypatch):
    factory = session_factory(settings, monkeypatch)
    connection_id, _mapping_id = connection_and_mapping(factory)
    transport = FakeTransport([])
    result = service(factory, settings, transport).discover(connection_id)
    assert result.status == "failed" and result.error_code == "configuration" and not transport.calls
    with factory() as session:
        health = session.get(IntegrationHealth, connection_id)
        assert health.auth_state == "not_configured" and health.discovery_state == "not_configured"


def test_mapping_not_found_and_duplicate_provider_id_remain_non_mutating(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = session_factory(settings, monkeypatch)
    connection_id, mapping_id = connection_and_mapping(factory)
    transport = FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        page([{"plantCode": "other"}]),
    ])
    result = service(factory, settings, transport).discover(connection_id)
    assert service(factory, settings, transport).validate_mapping(mapping_id, discovery=result).status is MappingValidationStatus.NOT_FOUND
    duplicate = replace(result, plants=(replace(result.plants[0], external_id="FS-001"), replace(result.plants[0], external_id="FS-001")), duplicate_external_ids=frozenset({"FS-001"}))
    assert service(factory, settings, transport).validate_mapping(mapping_id, discovery=duplicate).status is MappingValidationStatus.AMBIGUOUS_CONFLICT
    with factory() as session:
        assert session.get(AssetProviderMapping, mapping_id).mapping_status == "active"


def test_reconciliation_ignores_superseded_mapping_history(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = session_factory(settings, monkeypatch)
    connection_id, mapping_id = connection_and_mapping(factory)
    with factory() as session:
        previous_asset = create_asset(session, canonical_name="Former FusionSolar target")
        create_mapping(
            session,
            asset_id=previous_asset.id,
            provider_connection_id=connection_id,
            external_id="FS-001",
            mapping_status="superseded",
        )
        session.commit()
    transport = FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), page([{"plantCode": "FS-001"}])])
    result = service(factory, settings, transport).discover(connection_id)
    reconciliation = service(factory, settings, transport).reconcile(result)
    assert len(reconciliation) == 1
    assert reconciliation[0].status is ReconciliationStatus.MAPPED
    assert reconciliation[0].mapping_ids == (mapping_id,)


def test_transient_retry_is_bounded_and_each_network_call_is_accounted(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = session_factory(settings, monkeypatch)
    connection_id, _mapping_id = connection_and_mapping(factory)
    transport = FakeTransport([
        FusionSolarClientError(ProviderError(ProviderErrorCode.UNAVAILABLE, "outage", transient=True)),
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        page([{"plantCode": "FS-001"}]),
    ])
    result = service(factory, settings, transport, retries=1).discover(connection_id)
    assert result.status == "success" and len(transport.calls) == 3
    with factory() as session:
        attempts = list(session.scalars(select(ProviderRequestAttempt).where(ProviderRequestAttempt.sync_run_id == result.sync_run_id).order_by(ProviderRequestAttempt.id)))
        assert [attempt.status for attempt in attempts] == ["failed", "succeeded", "succeeded"]
