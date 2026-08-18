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
from nemsei.providers.models import OperatorAuditEvent
from nemsei.providers.preflight import activation_preflight
from nemsei.providers.registry import ProviderCapability
from nemsei.providers.service import approve_mapping, configure_connection, create_connection, create_mapping, reject_mapping, set_connection_enabled
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
