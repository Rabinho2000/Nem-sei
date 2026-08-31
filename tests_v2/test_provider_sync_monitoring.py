from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.monitoring.models import MonitoringObservation
from nemsei.monitoring.service import record_observation, record_production_fact
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.registry import (
    ImplementationSupport,
    ProviderCapability,
    ProviderCode,
    RuntimeAvailability,
    evaluate_capability,
)
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.sources.service import create_source_policy, resolve_source_policy
from nemsei.sync.models import IntegrationHealth, ProviderRequestAttempt, SyncCursor
from nemsei.sync.service import advance_cursor, finish_sync_run, record_health, record_request_result, reserve_request, start_sync_run
from tests_v2.test_migrations import upgrade


def session_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))()


def asset_mapping(session):
    asset = create_asset(session, canonical_name="Canonical test asset")
    connection = create_connection(
        session,
        provider_code="fusionsolar",
        connection_key="canonical-test",
        display_name="Canonical test connection",
        enabled=True,
        configuration_status="configured",
    )
    mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="plant-test")
    session.flush()
    return asset, connection, mapping


@pytest.mark.parametrize("provider", list(ProviderCode))
def test_capability_support_and_runtime_availability_are_separate(provider: ProviderCode) -> None:
    # The expectation comes from the registry's own declaration rather than a
    # list repeated here. It used to read "everything except SMA supports
    # discovery", which stopped being true the moment a provider arrived that
    # deliberately does not: `huawei_scada` has no account to enumerate -- a
    # dongle announces itself, one at a time. What this test is actually about
    # is that the two dimensions move independently, and that holds whatever
    # each provider declares.
    from nemsei.providers.registry import descriptor_for

    supported = ProviderCapability.DISCOVERY in descriptor_for(provider).implemented_capabilities
    expected = ImplementationSupport.SUPPORTED if supported else ImplementationSupport.UNSUPPORTED
    unconfigured = evaluate_capability(provider, ProviderCapability.DISCOVERY, connection_configured=False)
    assert unconfigured.implementation_support is expected
    assert unconfigured.runtime_availability is RuntimeAvailability.NOT_CONFIGURED
    configured = evaluate_capability(provider, ProviderCapability.DISCOVERY, connection_configured=True)
    assert configured.implementation_support is expected
    assert configured.runtime_availability is (RuntimeAvailability.AVAILABLE if expected is ImplementationSupport.SUPPORTED else RuntimeAvailability.UNKNOWN)


def test_integration_health_never_creates_a_fake_asset_offline_observation(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        asset, connection, mapping = asset_mapping(session)
        record_health(
            session,
            provider_connection_id=connection.id,
            provider_state="unavailable",
            error=ProviderError(ProviderErrorCode.UNAVAILABLE, "provider unavailable", transient=True),
        )
        session.commit()
        health = session.get(IntegrationHealth, connection.id)
        assert health.provider_state == "unavailable"
        assert session.scalar(select(func.count()).select_from(MonitoringObservation)) == 0
        observation, created = record_observation(
            session,
            asset_id=asset.id,
            provider_mapping_id=mapping.id,
            source_observation_key="explicit-offline",
            observed_at=utc_now(),
            condition="offline",
            freshness="stale",
            quality="partial",
            completeness="partial",
        )
        assert created and observation.condition == "offline" and observation.freshness == "stale"
        stale_operational, _ = record_observation(
            session,
            asset_id=asset.id,
            provider_mapping_id=mapping.id,
            source_observation_key="stale-but-operational",
            observed_at=utc_now(),
            condition="operational",
            freshness="stale",
            quality="partial",
            completeness="partial",
        )
        assert stale_operational.condition == "operational"


def test_sync_lifecycle_cursor_safety_and_partial_failure_states(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        _asset, connection, _mapping = asset_mapping(session)
        successful = start_sync_run(session, provider_connection_id=connection.id, capability="production_history")
        session.flush()
        finish_sync_run(session, run=successful, status="success", completeness="complete")
        cursor = advance_cursor(session, run=successful, cursor_key="daily", checkpoint={"page": "2"}, covered_through=utc_now())
        partial = start_sync_run(session, provider_connection_id=connection.id, capability="production_history")
        finish_sync_run(session, run=partial, status="partial", completeness="partial")
        with pytest.raises(ValueError, match="successful sync"):
            advance_cursor(session, run=partial, cursor_key="daily", checkpoint={"page": "3"}, covered_through=utc_now())
        failed = start_sync_run(session, provider_connection_id=connection.id, capability="production_history")
        finish_sync_run(session, run=failed, status="failed", error=ProviderError(ProviderErrorCode.TIMEOUT, "timed out", transient=True))
        deferred = start_sync_run(session, provider_connection_id=connection.id, capability="production_history")
        finish_sync_run(session, run=deferred, status="deferred", error=ProviderError(ProviderErrorCode.RATE_LIMITED, "deferred"))
        rate_limited = start_sync_run(session, provider_connection_id=connection.id, capability="production_history")
        finish_sync_run(session, run=rate_limited, status="rate_limited", error=ProviderError(ProviderErrorCode.RATE_LIMITED, "retry later", retry_after_seconds=60))
        session.commit()
        assert cursor.last_successful_run_id == successful.id
        assert session.get(SyncCursor, cursor.id).covered_through == cursor.covered_through
        assert {partial.status, failed.status, deferred.status, rate_limited.status} == {"partial", "failed", "deferred", "rate_limited"}


def test_request_attempts_preserve_unknown_quota_and_retry_after_deferral(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        _asset, connection, _mapping = asset_mapping(session)
        state, attempt, allowed = reserve_request(session, provider_connection_id=connection.id, endpoint_family="history", purpose="test")
        assert allowed and state.quota_known is False
        now = utc_now()
        record_request_result(session, state=state, attempt=attempt, error=ProviderError(ProviderErrorCode.RATE_LIMITED, "retry later", retry_after_seconds=60), now=now)
        session.flush()
        _state, deferred, allowed = reserve_request(session, provider_connection_id=connection.id, endpoint_family="history", purpose="test", now=now + timedelta(seconds=1))
        assert not allowed and deferred.status == "deferred"
        session.flush()
        assert session.scalar(select(func.count()).select_from(ProviderRequestAttempt)) == 2


def test_observation_idempotency_and_revision_history(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        asset, _connection, mapping = asset_mapping(session)
        first, created = record_observation(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_observation_key="obs-1", observed_at=utc_now(), condition="operational", freshness="fresh", quality="complete", completeness="complete")
        session.flush()
        same, created_again = record_observation(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_observation_key="obs-1", observed_at=first.observed_at, condition="operational", freshness="fresh", quality="complete", completeness="complete")
        corrected, corrected_created = record_observation(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_observation_key="obs-1", observed_at=first.observed_at, condition="warning", freshness="fresh", quality="complete", completeness="complete")
        session.flush()
        assert created and not created_again and same.id == first.id
        assert corrected_created and corrected.source_revision == 2 and corrected.supersedes_observation_id == first.id
        with pytest.raises(DatabaseError):
            session.execute(text("UPDATE monitoring_observations SET condition = 'fault' WHERE id = :id"), {"id": first.id})


def test_production_missing_is_distinct_from_zero_and_corrections_are_immutable(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        asset, _connection, mapping = asset_mapping(session)
        start, end = utc_now(), utc_now() + timedelta(hours=1)
        missing, created = record_production_fact(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_fact_key="energy-1", period_start=start, period_end=end, granularity="hour", value=None, unit="kWh", quality="missing", completeness="partial")
        session.flush()
        zero, zero_created = record_production_fact(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_fact_key="energy-1", period_start=start, period_end=end, granularity="hour", value=Decimal("0"), unit="kWh", quality="complete", completeness="complete")
        assert created and zero_created and zero.source_revision == 2 and zero.supersedes_fact_id == missing.id
        with pytest.raises(ValueError, match="missing production"):
            record_production_fact(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_fact_key="invalid", period_start=start, period_end=end, granularity="hour", value=None, unit="kWh", quality="complete", completeness="complete")


def test_temporal_source_policy_detects_competing_primary_sources(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        asset, connection, first = asset_mapping(session)
        second = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="plant-test-2")
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=first.id, source_use="monitoring", priority=1, valid_from=date(2026, 1, 1))
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=second.id, source_use="monitoring", priority=1, valid_from=date(2026, 1, 1))
        session.flush()
        with pytest.raises(ValueError, match="Competing"):
            resolve_source_policy(session, asset_id=asset.id, source_use="monitoring", on_date=date(2026, 2, 1))
