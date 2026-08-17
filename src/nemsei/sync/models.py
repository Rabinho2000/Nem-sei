from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


HEALTH_STATES = ("unknown", "healthy", "unavailable", "degraded", "not_configured")
SYNC_RUN_STATUSES = ("pending", "running", "success", "partial", "failed", "deferred", "rate_limited")
REQUEST_ATTEMPT_STATUSES = ("reserved", "called", "succeeded", "failed", "deferred", "rate_limited")


class IntegrationHealth(Base):
    __tablename__ = "integration_health"
    __table_args__ = (
        CheckConstraint(f"auth_state IN {HEALTH_STATES!r}", name="ck_integration_health_auth"),
        CheckConstraint(f"access_state IN {HEALTH_STATES!r}", name="ck_integration_health_access"),
        CheckConstraint(f"provider_state IN {HEALTH_STATES!r}", name="ck_integration_health_provider"),
        CheckConstraint(f"discovery_state IN {HEALTH_STATES!r}", name="ck_integration_health_discovery"),
        CheckConstraint(f"quota_state IN {HEALTH_STATES!r}", name="ck_integration_health_quota"),
        CheckConstraint(f"sync_state IN {HEALTH_STATES!r}", name="ck_integration_health_sync"),
    )

    provider_connection_id: Mapped[int] = mapped_column(ForeignKey("provider_connections.id", ondelete="CASCADE"), primary_key=True)
    auth_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    access_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    provider_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    discovery_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    quota_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    sync_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(f"status IN {SYNC_RUN_STATUSES!r}", name="ck_sync_runs_status"),
        Index("ix_sync_runs_connection_capability", "provider_connection_id", "capability", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_connection_id: Mapped[int] = mapped_column(ForeignKey("provider_connections.id", ondelete="RESTRICT"), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    requested_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completeness: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    error_code: Mapped[str | None] = mapped_column(String(48))
    safe_detail: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (UniqueConstraint("provider_connection_id", "capability", "cursor_key", name="uq_sync_cursors_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_connection_id: Mapped[int] = mapped_column(ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    covered_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderRequestState(Base):
    __tablename__ = "provider_request_states"
    __table_args__ = (UniqueConstraint("provider_connection_id", "endpoint_family", name="uq_provider_request_state_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_connection_id: Mapped[int] = mapped_column(ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False)
    endpoint_family: Mapped[str] = mapped_column(String(80), nullable=False)
    quota_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_allowed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderRequestAttempt(Base):
    __tablename__ = "provider_request_attempts"
    __table_args__ = (
        CheckConstraint(f"status IN {REQUEST_ATTEMPT_STATUSES!r}", name="ck_provider_request_attempts_status"),
        Index("ix_provider_request_attempts_state_time", "request_state_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_state_id: Mapped[int] = mapped_column(ForeignKey("provider_request_states.id", ondelete="RESTRICT"), nullable=False)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="SET NULL"))
    purpose: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_detail: Mapped[str | None] = mapped_column(Text)
