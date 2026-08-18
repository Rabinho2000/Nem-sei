from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy import text

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.discovery import DiscoveryResult, DiscoveredPlant, MappingValidation, MappingValidationStatus
from nemsei.integrations.fusionsolar.monitoring import MonitoringSyncResult
from nemsei.integrations.fusionsolar.production import ProductionSyncResult
from nemsei.integrations.fusionsolar.validation import FusionSolarSingleAssetValidation
from nemsei.providers.models import OperatorAuditEvent
from nemsei.providers.preflight import activation_preflight
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.providers.service import approve_mapping, configure_connection, create_connection, create_mapping, reject_mapping, set_connection_enabled
from nemsei.shared.clock import utc_now
from nemsei.sources.service import create_source_policy
from tests_v2.test_migrations import upgrade


def session_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))()


def pending_fixture(session):
    asset = create_asset(session, canonical_name="Reviewed plant", timezone="Europe/Lisbon")
    connection = create_connection(session, provider_code="fusionsolar", connection_key="primary", display_name="Primary")
    mapping = create_mapping(
        session,
        asset_id=asset.id,
        provider_connection_id=connection.id,
        external_id="FS-PRIMARY",
        mapping_status="pending_review",
    )
    session.flush()
    return asset, connection, mapping


def activate_fixture(session, actor="operator"):
    asset, connection, mapping = pending_fixture(session)
    configure_connection(session, connection_id=connection.id, credential_reference="primary", actor_username=actor)
    set_connection_enabled(session, connection_id=connection.id, enabled=True, actor_username=actor)
    approve_mapping(session, mapping_id=mapping.id, actor_username=actor)
    create_source_policy(
        session,
        asset_id=asset.id,
        provider_mapping_id=mapping.id,
        source_use="monitoring",
        priority=1,
        valid_from=date(2026, 1, 1),
        actor_username=actor,
    )
    session.flush()
    return asset, connection, mapping


def test_approval_requires_connection_and_asset_review(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        asset, _connection, mapping = pending_fixture(session)
        with pytest.raises(ValueError, match="identity review"):
            asset.review_status = "needs_review"
            approve_mapping(session, mapping_id=mapping.id, actor_username="operator")


def test_explicit_configuration_enable_approval_and_audit(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        asset, connection, mapping = pending_fixture(session)
        configure_connection(session, connection_id=connection.id, credential_reference="primary", actor_username="operator")
        set_connection_enabled(session, connection_id=connection.id, enabled=True, actor_username="operator")
        approve_mapping(session, mapping_id=mapping.id, actor_username="operator")
        session.commit()
        assert mapping.mapping_status == "active"
        assert connection.enabled is True
        actions = list(session.scalars(select(OperatorAuditEvent.action).order_by(OperatorAuditEvent.id)))
        assert actions == ["connection_configured", "connection_enabled", "mapping_approved"]


def test_preflight_blocks_global_provider_reads_and_missing_policy(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        _asset, _connection, mapping = activate_fixture(session)
        result = activation_preflight(session, settings=settings, mapping_id=mapping.id, capability=ProviderCapability.CURRENT_MONITORING)
        codes = {finding.code for finding in result.blocking_findings}
        assert not result.ready
        assert "provider_reads_disabled" in codes
        assert result.implementation_support == "supported"
        assert result.runtime_availability == "unknown"


def test_preflight_ready_is_provider_neutral_and_network_free(settings, monkeypatch):
    enabled = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    with session_for(enabled, monkeypatch) as session:
        _asset, _connection, mapping = activate_fixture(session)
        result = activation_preflight(session, settings=enabled, mapping_id=mapping.id, capability=ProviderCapability.CURRENT_MONITORING)
        assert result.ready
        assert result.implementation_support == "supported"
        assert result.runtime_availability == "available"
        assert _connection.provider_code == "fusionsolar"


def test_mapping_rejection_is_explicit_and_audited(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        _asset, _connection, mapping = pending_fixture(session)
        reject_mapping(session, mapping_id=mapping.id, actor_username="operator")
        session.commit()
        assert mapping.mapping_status == "invalid"
        assert session.scalar(select(OperatorAuditEvent.action)) == "mapping_rejected"


def test_operator_audit_is_append_only_and_sanitized(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        _asset, _connection, _mapping = activate_fixture(session)
        event = session.scalar(select(OperatorAuditEvent).order_by(OperatorAuditEvent.id))
        assert event is not None
        assert "credential" not in event.metadata_json
        with pytest.raises(DatabaseError):
            session.execute(text("UPDATE operator_audit_events SET action = 'connection_disabled' WHERE id = :id"), {"id": event.id})
        with pytest.raises(DatabaseError):
            session.execute(text("DELETE FROM operator_audit_events WHERE id = :id"), {"id": event.id})


def test_mapping_preflight_and_source_policy_pages_are_authenticated_and_operational(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        _asset, _connection, mapping = activate_fixture(session)
        session.commit()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True
        browser_session["username"] = "operator"
    mapping_page = client.get("/mappings?status=active")
    preflight_page = client.get(f"/mappings/{mapping.id}/preflight?capability=current_monitoring")
    policy_page = client.get("/source-policies")
    assert mapping_page.status_code == 200
    assert "FS-PRIMARY" in mapping_page.text
    assert preflight_page.status_code == 200
    assert "provider_reads_disabled" in preflight_page.text
    assert policy_page.status_code == 200


def test_production_preflight_requires_verified_contract_and_timezone(settings, monkeypatch):
    enabled = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    with session_for(enabled, monkeypatch) as session:
        asset, _connection, mapping = activate_fixture(session)
        create_source_policy(
            session,
            asset_id=asset.id,
            provider_mapping_id=mapping.id,
            source_use="production",
            priority=1,
            valid_from=date(2026, 1, 1),
        )
        result = activation_preflight(session, settings=enabled, mapping_id=mapping.id, capability=ProviderCapability.PRODUCTION_HISTORY)
        assert not result.ready
        assert "production_contract_missing" in {finding.code for finding in result.blocking_findings}
        asset.timezone = "Not/AZone"
        invalid = activation_preflight(session, settings=enabled, mapping_id=mapping.id, capability=ProviderCapability.PRODUCTION_HISTORY)
        assert "timezone_invalid" in {finding.code for finding in invalid.blocking_findings}


def test_single_asset_validation_blocks_before_network_and_audits(settings, monkeypatch):
    def should_not_construct(*_args, **_kwargs):
        raise AssertionError("provider service must not be constructed when preflight is blocked")

    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    with factory() as session:
        _asset, _connection, mapping = activate_fixture(session)
        session.commit()
        result = FusionSolarSingleAssetValidation(
            factory,
            settings,
            discovery_factory=should_not_construct,
            monitoring_factory=should_not_construct,
            production_factory=should_not_construct,
        ).run(mapping.id, actor_username="operator")
        assert result.status == "blocked"
        assert result.provider_calls == 0
        assert "provider_reads_disabled" in result.findings
        assert session.scalar(select(OperatorAuditEvent).where(OperatorAuditEvent.action == "validation_requested")) is not None


def test_single_asset_validation_is_explicit_and_scoped(settings, monkeypatch):
    enabled = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRIMARY_PRODUCTION_TIMEZONE", "Europe/Lisbon")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRIMARY_PRODUCTION_UNIT", "kWh")

    class FakeDiscovery:
        def validate_connection(self, connection_id):
            return DiscoveryResult(connection_id, (DiscoveredPlant(ProviderCode.FUSIONSOLAR, connection_id, "FS-PRIMARY", "Primary", {}, utc_now()),), frozenset(), "success", "partial", 101)

        def validate_mapping(self, mapping_id, *, discovery):
            return MappingValidation(mapping_id, MappingValidationStatus.VALID, discovery.sync_run_id)

    class FakeMonitoring:
        def sync_current_monitoring(self, connection_id):
            return MonitoringSyncResult(connection_id, 102, "success", "complete", 1, 1, 1, 0)

    class FakeProduction:
        def sync_incremental(self, connection_id, *, start_date, end_date):
            assert start_date == end_date
            return ProductionSyncResult(connection_id, 103, "success", "complete", start_date, end_date, 1, 1, 1, 0, True)

    upgrade(enabled, monkeypatch)
    factory = build_session_factory(build_engine(enabled))
    with factory() as session:
        asset, connection, mapping = activate_fixture(session)
        create_source_policy(
            session,
            asset_id=asset.id,
            provider_mapping_id=mapping.id,
            source_use="production",
            priority=1,
            valid_from=date(2026, 1, 1),
        )
        session.commit()
        result = FusionSolarSingleAssetValidation(
            factory,
            enabled,
            discovery_factory=lambda *_args: FakeDiscovery(),
            monitoring_factory=lambda *_args: FakeMonitoring(),
            production_factory=lambda *_args: FakeProduction(),
        ).run(mapping.id, actor_username="operator", on_date=date(2026, 8, 18))
        assert result.status == "success"
        assert result.discovery_status == "success"
        assert result.mapping_status == "valid"
        assert result.monitoring_status == "success"
        assert result.production_status == "success"
        assert result.provider_calls == 0  # fakes perform no network calls
        actions = list(session.scalars(select(OperatorAuditEvent.action).where(OperatorAuditEvent.action == "validation_requested")))
        assert len(actions) == 2
