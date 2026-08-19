from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


OBSERVATION_CONDITIONS = ("operational", "warning", "fault", "offline", "unknown")
FRESHNESS_STATES = ("fresh", "stale", "unknown")
QUALITY_STATES = ("complete", "partial", "missing", "invalid", "unknown")
# The energy signals a report needs. Each one is a metric a provider states in
# its own payload; none is derived from another, because the identity
# production = self_use + export holds for FusionSolar and does *not* hold for
# Sigenergy, where a battery absorbs the difference. Deriving would quietly
# invent a number for every plant that stores energy.
PRODUCTION_METRICS = (
    "production_energy",
    "self_use_energy",
    "export_energy",
    "consumption_energy",
    "grid_import_energy",
)


class MonitoringObservation(Base):
    __tablename__ = "monitoring_observations"
    __table_args__ = (
        CheckConstraint(f"condition IN {OBSERVATION_CONDITIONS!r}", name="ck_monitoring_observations_condition"),
        CheckConstraint(f"freshness IN {FRESHNESS_STATES!r}", name="ck_monitoring_observations_freshness"),
        CheckConstraint(f"quality IN {QUALITY_STATES!r}", name="ck_monitoring_observations_quality"),
        CheckConstraint(f"completeness IN {QUALITY_STATES!r}", name="ck_monitoring_observations_completeness"),
        UniqueConstraint("provider_mapping_id", "source_observation_key", "source_revision", name="uq_monitoring_observation_revision"),
        Index("ix_monitoring_observations_asset_time", "asset_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    provider_mapping_id: Mapped[int] = mapped_column(ForeignKey("asset_provider_mappings.id", ondelete="RESTRICT"), nullable=False)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="SET NULL"))
    source_observation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_observation_id: Mapped[int | None] = mapped_column(ForeignKey("monitoring_observations.id", ondelete="RESTRICT"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    condition: Mapped[str] = mapped_column(String(24), nullable=False)
    freshness: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    quality: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    completeness: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    raw_status_code: Mapped[str | None] = mapped_column(String(120))
    raw_status_text: Mapped[str | None] = mapped_column(String(500))
    safe_detail: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class MonitoringCurrentState(Base):
    """Mutable per-mapping confirmation evidence, separate from fact history."""

    __tablename__ = "monitoring_current_states"

    provider_mapping_id: Mapped[int] = mapped_column(
        ForeignKey("asset_provider_mappings.id", ondelete="CASCADE"), primary_key=True
    )
    latest_observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitoring_observations.id", ondelete="RESTRICT")
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductionFact(Base):
    __tablename__ = "production_facts"
    __table_args__ = (
        CheckConstraint(f"metric_kind IN {PRODUCTION_METRICS!r}", name="ck_production_facts_metric"),
        CheckConstraint(f"quality IN {QUALITY_STATES!r}", name="ck_production_facts_quality"),
        CheckConstraint(f"completeness IN {QUALITY_STATES!r}", name="ck_production_facts_completeness"),
        CheckConstraint("value IS NOT NULL OR quality = 'missing'", name="ck_production_facts_missing_value"),
        UniqueConstraint("provider_mapping_id", "source_fact_key", "source_revision", name="uq_production_fact_revision"),
        Index("ix_production_facts_asset_period", "asset_id", "period_start", "period_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    provider_mapping_id: Mapped[int] = mapped_column(ForeignKey("asset_provider_mappings.id", ondelete="RESTRICT"), nullable=False)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="SET NULL"))
    source_fact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_fact_id: Mapped[int | None] = mapped_column(ForeignKey("production_facts.id", ondelete="RESTRICT"))
    metric_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="production_energy")
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granularity: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    quality: Mapped[str] = mapped_column(String(24), nullable=False)
    completeness: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
