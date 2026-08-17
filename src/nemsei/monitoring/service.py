"""Append-only canonical fact persistence without provider client dependencies."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from nemsei.monitoring.models import (
    FRESHNESS_STATES,
    OBSERVATION_CONDITIONS,
    PRODUCTION_METRICS,
    QUALITY_STATES,
    MonitoringObservation,
    ProductionFact,
)
from nemsei.providers.models import AssetProviderMapping
from nemsei.monitoring.repository import CanonicalFactRepository
from nemsei.shared.clock import as_utc, utc_now


def _mapping_for_asset(session: Session, *, asset_id: int, provider_mapping_id: int) -> AssetProviderMapping:
    mapping = session.get(AssetProviderMapping, provider_mapping_id)
    if mapping is None or mapping.asset_id != asset_id:
        raise ValueError("Canonical fact mapping must belong to its asset")
    return mapping


def record_observation(
    session: Session,
    *,
    asset_id: int,
    provider_mapping_id: int,
    source_observation_key: str,
    observed_at: datetime,
    condition: str,
    freshness: str = "unknown",
    quality: str = "unknown",
    completeness: str = "unknown",
    sync_run_id: int | None = None,
    raw_status_code: str | None = None,
    raw_status_text: str | None = None,
    safe_detail: str | None = None,
    metadata: dict[str, Any] | None = None,
    deduplicate_observed_at: bool = False,
) -> tuple[MonitoringObservation, bool]:
    if condition not in OBSERVATION_CONDITIONS or freshness not in FRESHNESS_STATES or quality not in QUALITY_STATES or completeness not in QUALITY_STATES:
        raise ValueError("Invalid canonical observation state")
    _mapping_for_asset(session, asset_id=asset_id, provider_mapping_id=provider_mapping_id)
    key = source_observation_key.strip()
    if not key:
        raise ValueError("Source observation key is required")
    existing = CanonicalFactRepository(session).latest_observation(provider_mapping_id=provider_mapping_id, source_key=key)
    normalized = (
        None if deduplicate_observed_at else as_utc(observed_at),
        condition,
        freshness,
        quality,
        completeness,
        raw_status_code,
        raw_status_text,
        metadata or {},
    )
    existing_normalized = (
        None if deduplicate_observed_at else as_utc(existing.observed_at),
        existing.condition,
        existing.freshness,
        existing.quality,
        existing.completeness,
        existing.raw_status_code,
        existing.raw_status_text,
        existing.metadata_json,
    ) if existing else None
    if existing and normalized == existing_normalized:
        return existing, False
    revision = (existing.source_revision + 1) if existing else 1
    observation = MonitoringObservation(
        asset_id=asset_id,
        provider_mapping_id=provider_mapping_id,
        sync_run_id=sync_run_id,
        source_observation_key=key,
        source_revision=revision,
        supersedes_observation_id=existing.id if existing else None,
        observed_at=as_utc(observed_at),
        ingested_at=utc_now(),
        condition=condition,
        freshness=freshness,
        quality=quality,
        completeness=completeness,
        raw_status_code=raw_status_code[:120] if raw_status_code else None,
        raw_status_text=raw_status_text[:500] if raw_status_text else None,
        safe_detail=safe_detail[:500] if safe_detail else None,
        metadata_json=dict(metadata or {}),
    )
    session.add(observation)
    return observation, True


def record_production_fact(
    session: Session,
    *,
    asset_id: int,
    provider_mapping_id: int,
    source_fact_key: str,
    period_start: datetime,
    period_end: datetime,
    granularity: str,
    value: Decimal | None,
    unit: str,
    quality: str,
    completeness: str,
    metric_kind: str = "production_energy",
    sync_run_id: int | None = None,
) -> tuple[ProductionFact, bool]:
    if metric_kind not in PRODUCTION_METRICS or quality not in QUALITY_STATES or completeness not in QUALITY_STATES:
        raise ValueError("Invalid production fact state")
    if value is None and quality != "missing":
        raise ValueError("A missing production value must use missing quality")
    if value is not None and quality == "missing":
        raise ValueError("A numeric production value cannot use missing quality")
    if as_utc(period_end) < as_utc(period_start):
        raise ValueError("Production fact period is invalid")
    _mapping_for_asset(session, asset_id=asset_id, provider_mapping_id=provider_mapping_id)
    key = source_fact_key.strip()
    if not key or not unit.strip() or not granularity.strip():
        raise ValueError("Production fact key, unit, and granularity are required")
    existing = CanonicalFactRepository(session).latest_production_fact(provider_mapping_id=provider_mapping_id, source_key=key)
    normalized = (as_utc(period_start), as_utc(period_end), granularity, value, unit, quality, completeness, metric_kind)
    if existing and normalized == (as_utc(existing.period_start), as_utc(existing.period_end), existing.granularity, existing.value, existing.unit, existing.quality, existing.completeness, existing.metric_kind):
        return existing, False
    fact = ProductionFact(
        asset_id=asset_id,
        provider_mapping_id=provider_mapping_id,
        sync_run_id=sync_run_id,
        source_fact_key=key,
        source_revision=(existing.source_revision + 1) if existing else 1,
        supersedes_fact_id=existing.id if existing else None,
        metric_kind=metric_kind,
        period_start=as_utc(period_start),
        period_end=as_utc(period_end),
        granularity=granularity.strip(),
        value=value,
        unit=unit.strip(),
        quality=quality,
        completeness=completeness,
        ingested_at=utc_now(),
        metadata_json={},
    )
    session.add(fact)
    return fact, True
