"""The persisted record of one attempt to satisfy one collection obligation.

Kept in its own module rather than added to `sync/models.py` because a
collection run is not a provider concept. `SyncRun` answers "did we talk to
the provider and how did that go"; a collection run answers "did the data we
owed actually land". A run can be `fulfilled` with no provider call at all
(the Huawei rollup derives locally), and a `SyncRun` can be `success` while
its collection run is `partial`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_FULFILLED = "fulfilled"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_LOST_OWNERSHIP = "lost_ownership"
STATUS_CANCELLED = "cancelled"
STATUS_SUPERSEDED = "superseded"

COLLECTION_RUN_STATUSES = (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_FULFILLED,
    STATUS_PARTIAL,
    STATUS_FAILED,
    STATUS_LOST_OWNERSHIP,
    STATUS_CANCELLED,
    STATUS_SUPERSEDED,
)

# Terminal in the sense that matters: a run in one of these will never become
# `fulfilled`. Repairing the scope means a *new* run, at `attempt + 1`, so the
# record of what went wrong survives instead of being overwritten by whatever
# eventually worked.
TERMINAL_STATUSES = (
    STATUS_FULFILLED,
    STATUS_PARTIAL,
    STATUS_FAILED,
    STATUS_LOST_OWNERSHIP,
    STATUS_CANCELLED,
    STATUS_SUPERSEDED,
)

# The only transitions the service will perform. `failed -> fulfilled` is
# absent on purpose and is the single most important absence here.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_PENDING: (STATUS_RUNNING, STATUS_CANCELLED, STATUS_SUPERSEDED),
    STATUS_RUNNING: (
        STATUS_FULFILLED,
        STATUS_PARTIAL,
        STATUS_FAILED,
        STATUS_LOST_OWNERSHIP,
        STATUS_CANCELLED,
    ),
    STATUS_FULFILLED: (),
    STATUS_PARTIAL: (STATUS_SUPERSEDED,),
    STATUS_FAILED: (STATUS_SUPERSEDED,),
    STATUS_LOST_OWNERSHIP: (STATUS_SUPERSEDED,),
    STATUS_CANCELLED: (STATUS_SUPERSEDED,),
    STATUS_SUPERSEDED: (),
}


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint(f"status IN {COLLECTION_RUN_STATUSES!r}", name="ck_collection_runs_status"),
        CheckConstraint("period_start <= period_end", name="ck_collection_runs_period"),
        CheckConstraint("attempt >= 1", name="ck_collection_runs_attempt"),
        CheckConstraint("facts_written >= 0", name="ck_collection_runs_facts_written"),
        CheckConstraint("scopes_written >= 0", name="ck_collection_runs_scopes_written"),
        CheckConstraint(
            "scopes_required IS NULL OR scopes_required >= 0",
            name="ck_collection_runs_scopes_required",
        ),
        CheckConstraint(
            "scopes_required IS NULL OR scopes_written <= scopes_required",
            name="ck_collection_runs_scopes_within_required",
        ),
        CheckConstraint(
            "status <> 'fulfilled' OR ("
            " lease_generation IS NOT NULL"
            " AND scopes_required IS NOT NULL"
            " AND scopes_written = scopes_required"
            " AND finished_at IS NOT NULL"
            ")",
            name="ck_collection_runs_fulfilled_evidence",
        ),
        Index(
            "uq_collection_runs_fulfilled_scope",
            "provider_connection_id",
            "capability",
            "scope_kind",
            "scope_key",
            "period_start",
            "period_end",
            unique=True,
            postgresql_where=text("status = 'fulfilled'"),
        ),
        Index(
            "ix_collection_runs_scope_period",
            "provider_connection_id",
            "capability",
            "scope_kind",
            "scope_key",
            "period_start",
        ),
        Index("ix_collection_runs_job", "job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_connection_id: Mapped[int] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=STATUS_PENDING)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    lease_generation: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    facts_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scopes_required: Mapped[int | None] = mapped_column(Integer)
    scopes_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor_advanced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @property
    def fulfilled(self) -> bool:
        return self.status == STATUS_FULFILLED

    def as_evidence(self) -> dict[str, Any]:
        """What this run can show for itself, for logs and screens."""
        return {
            "collection_run_id": self.id,
            "status": self.status,
            "attempt": self.attempt,
            "facts_written": self.facts_written,
            "scopes_required": self.scopes_required,
            "scopes_written": self.scopes_written,
            "cursor_advanced": self.cursor_advanced,
            "lease_generation": self.lease_generation,
            "error_code": self.error_code,
        }
