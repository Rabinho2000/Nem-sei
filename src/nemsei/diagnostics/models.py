"""Device status facts: point-in-time diagnostic evidence, per device.

Separate from `reporting/`, deliberately. Diagnosis is equipment state — is
this inverter communicating, is it producing, is it available — and reporting
is money and contracted energy. `production_facts` already keeps these apart
by `metric_kind`; this module keeps them apart by living in its own package,
so a future field never has to choose which layer it "also" belongs to.

Anchored on `device_id`, not `provider_mapping_id`. Every device-scoped
provider mapping V1 left behind is `pending_review` on a disabled,
credential-free legacy connection — none is active — so a table requiring a
usable mapping could not accept a single row today. `device_id` is what the M1
identity import already resolved for every device, independent of whether any
provider connection is currently usable.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


# V1's own vocabulary (`services/fusionsolar.py:classify_fusionsolar_inverter_availability`),
# kept verbatim rather than approximated onto `monitoring.OBSERVATION_CONDITIONS`,
# which was designed for provider-connection observations, not physical
# inverter states, and has no honest mapping for "standby".
AVAILABILITY_STATES = ("available", "standby", "unavailable", "unknown")
SOURCE_KINDS = ("v1_import", "live_read")
# Same vocabulary as monitoring.FRESHNESS_STATES/QUALITY_STATES (migration
# 0016). Added for Fatia 2's live reads; every Fatia 1 (`v1_import`) row
# defaults to `unknown` on all three rather than a guessed value, since V1
# recorded neither.
FRESHNESS_STATES = ("fresh", "stale", "unknown")
QUALITY_STATES = ("complete", "partial", "missing", "invalid", "unknown")


class DeviceStatusFact(Base):
    """One point-in-time reading of one device's status, power and energy."""

    __tablename__ = "device_status_facts"
    __table_args__ = (
        CheckConstraint(f"availability_status IN {AVAILABILITY_STATES!r}", name="ck_device_status_facts_availability"),
        CheckConstraint(f"source_kind IN {SOURCE_KINDS!r}", name="ck_device_status_facts_source_kind"),
        CheckConstraint(f"freshness IN {FRESHNESS_STATES!r}", name="ck_device_status_facts_freshness"),
        CheckConstraint(f"quality IN {QUALITY_STATES!r}", name="ck_device_status_facts_quality"),
        CheckConstraint(f"completeness IN {QUALITY_STATES!r}", name="ck_device_status_facts_completeness"),
        CheckConstraint("active_power_kw IS NULL OR active_power_kw >= 0", name="ck_device_status_facts_power"),
        CheckConstraint("day_energy_kwh IS NULL OR day_energy_kwh >= 0", name="ck_device_status_facts_energy"),
        UniqueConstraint("device_id", "source_fact_key", "source_revision", name="uq_device_status_facts_revision"),
        Index("ix_device_status_facts_device_time", "device_id", "observed_at"),
        Index("ix_device_status_facts_asset_time", "asset_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    source_fact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_fact_id: Mapped[int | None] = mapped_column(ForeignKey("device_status_facts.id", ondelete="RESTRICT"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    availability_status: Mapped[str] = mapped_column(String(24), nullable=False)
    active_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    day_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="v1_import")
    freshness: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    quality: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    completeness: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="SET NULL"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


# M7 Fatia 5 (D1, docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md): a persisted,
# deduplicated occurrence of one diagnostics/findings.py rule for one
# (rule_code, asset_id, device_id). Deliberately does not replace findings.py --
# that module stays exactly as it was (recomputed per request, no persistence)
# because the UI's "what is true right now" question does not need memory.
# This table exists only for what genuinely needs memory: has this exact
# problem already been seen (dedup by episode, not by every re-evaluation),
# when did it start, is it still going, and when did it stop -- the questions
# notifications (a later slice) cannot answer from findings.py alone.
INCIDENT_STATUSES = ("open", "resolved")


class DiagnosticIncident(Base):
    """One deduplicated episode of a diagnostics rule being true.

    Identity is `(rule_code, asset_id, device_id)` -- the same triple that
    already orders findings today, not a new notion invented for this table.
    Only one `open` incident may exist per identity at a time (enforced by a
    partial unique index, not just application logic); a resolved incident
    reopening later is a new row with a new `opened_at`, not a reused one --
    each episode gets its own row and its own duration.
    """

    __tablename__ = "diagnostic_incidents"
    __table_args__ = (
        CheckConstraint(f"status IN {INCIDENT_STATUSES!r}", name="ck_diagnostic_incidents_status"),
        CheckConstraint("(status = 'resolved') = (resolved_at IS NOT NULL)", name="ck_diagnostic_incidents_resolved_at"),
        CheckConstraint("occurrence_count >= 1", name="ck_diagnostic_incidents_occurrence_count"),
        CheckConstraint("last_observed_at >= opened_at", name="ck_diagnostic_incidents_observed_after_open"),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= last_observed_at", name="ck_diagnostic_incidents_resolved_after_observed"
        ),
        # A functional index, not a plain UniqueConstraint: Postgres treats
        # every NULL as distinct from every other NULL, so a plain unique
        # constraint on (rule_code, asset_id, device_id) would silently let
        # two "open" asset-level incidents (device_id IS NULL, e.g.
        # partial_device_coverage) coexist for the same asset -- the exact
        # duplication this table exists to prevent. `COALESCE(device_id, -1)`
        # closes that gap; no real device has id -1.
        Index(
            "uq_diagnostic_incidents_open_identity",
            "rule_code",
            "asset_id",
            text("coalesce(device_id, -1)"),
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_diagnostic_incidents_status", "status", "last_observed_at"),
        Index("ix_diagnostic_incidents_asset", "asset_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="RESTRICT"))
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    # First detection: the earliest evidence available for this episode --
    # a device-state rule's real `active_since` (walked from history) when
    # the detector has one, or the evaluation run's own timestamp otherwise.
    # Never re-derived once set; an episode's start does not move.
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Last confirmation: updated every evaluation run that still finds the
    # same rule true for the same identity. This is what "still active" and
    # "how stale is this incident" are read from.
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How many separate evaluation runs have confirmed this episode, starting
    # at 1 on creation. Deliberately an evaluator-cycle counter, not a count
    # of raw device_status_facts rows -- V1's `count_problem_occurrences_since`
    # counted raw rows, which would misfire as "worse" purely because V2 polls
    # far more densely than V1 ever did. See DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md.
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Which version of the rule set (diagnostics.findings.RULES_VERSION)
    # detected this episode -- provenance for "which regra originated this",
    # independent of rule_code, so a rule's logic changing later does not
    # silently reinterpret old incidents as if the new logic had always applied.
    detector_version: Mapped[str] = mapped_column(String(16), nullable=False)
    # A snapshot of the triggering DiagnosticFinding's own evidence, refreshed
    # on every re-observation -- the same dict findings.py already builds,
    # never a second, independent description of the same fact.
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
