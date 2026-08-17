from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.monitoring.models import ProductionFact
from nemsei.monitoring.production_coverage import production_coverage
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from tests_v2.test_migrations import upgrade


def session_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))()


def mapping(session):
    asset = create_asset(session, canonical_name="Evidence asset")
    connection = create_connection(session, provider_code="fusionsolar", connection_key="evidence", display_name="Evidence", enabled=True, configuration_status="configured")
    return asset, create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="EVIDENCE")


def record(session, asset_id, mapping_id, *, value, quality, completeness, sync_run_id=None):
    day = date(2026, 1, 15)
    return record_production_fact(
        session, asset_id=asset_id, provider_mapping_id=mapping_id, source_fact_key="evidence-day",
        period_start=datetime(2026, 1, 15, tzinfo=timezone.utc), period_end=datetime(2026, 1, 16, tzinfo=timezone.utc),
        granularity="day", value=value, unit="kWh", quality=quality, completeness=completeness,
        sync_run_id=sync_run_id, metadata={"source_period_timezone": "UTC", "source_period_date": day.isoformat()},
    )


def test_latest_missing_evidence_and_effective_complete_value_are_independent(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        asset, provider_mapping = mapping(session)
        first, _ = record(session, asset.id, provider_mapping.id, value=Decimal("120"), quality="complete", completeness="complete")
        missing, created = record(session, asset.id, provider_mapping.id, value=None, quality="missing", completeness="partial")
        same_missing, created_again = record(session, asset.id, provider_mapping.id, value=None, quality="missing", completeness="partial")
        session.flush()
        coverage = production_coverage(session, provider_mapping_id=provider_mapping.id, source_timezone="UTC", start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))[0]
        assert created and not created_again and same_missing.id == missing.id
        assert coverage.latest_evidence_status == "missing" and coverage.latest_evidence_fact_id == missing.id
        assert coverage.effective_complete_fact_id == first.id and coverage.effective_complete_value == Decimal("120")
        assert len(list(session.scalars(select(ProductionFact)))) == 2


def test_later_complete_revision_replaces_effective_complete_and_zero_is_complete(settings, monkeypatch):
    with session_for(settings, monkeypatch) as session:
        asset, provider_mapping = mapping(session)
        first, _ = record(session, asset.id, provider_mapping.id, value=Decimal("120"), quality="complete", completeness="complete")
        missing, _ = record(session, asset.id, provider_mapping.id, value=None, quality="missing", completeness="partial")
        corrected, _ = record(session, asset.id, provider_mapping.id, value=Decimal("121"), quality="complete", completeness="complete")
        session.flush()
        coverage = production_coverage(session, provider_mapping_id=provider_mapping.id, source_timezone="UTC", start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))[0]
        assert corrected.supersedes_fact_id == missing.id and corrected.source_revision == 3
        assert coverage.latest_evidence_status == "complete" and coverage.effective_complete_fact_id == corrected.id
        assert coverage.effective_complete_value == Decimal("121")
        zero_asset = create_asset(session, canonical_name="Zero evidence")
        zero_mapping = create_mapping(session, asset_id=zero_asset.id, provider_connection_id=provider_mapping.provider_connection_id, external_id="ZERO")
        zero, _ = record(session, zero_asset.id, zero_mapping.id, value=Decimal("0"), quality="complete", completeness="complete")
        session.flush()
        zero_coverage = production_coverage(session, provider_mapping_id=zero_mapping.id, source_timezone="UTC", start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))[0]
        assert zero_coverage.latest_evidence_status == "complete" and zero_coverage.effective_complete_fact_id == zero.id
        assert zero_coverage.effective_complete_value == Decimal("0")
