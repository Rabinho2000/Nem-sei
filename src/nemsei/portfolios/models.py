"""Portfolios: flat collections of installations, with history.

The shape is derived from V1's real data rather than from its screens — see
`docs/v2/PORTFOLIOS.md` for the survey. Two properties of that data are load
bearing here.

A member is not always an asset. V1 carries 23 members with a name, a NIF and a
sub-account but no installation, and they are members: `asset_id` is nullable and
`resolution_state` says which kind of member this is.

A portfolio has no parent. There is no column for one, which is the strongest
available way to say that portfolios do not nest.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nemsei.assets.models import public_id
from nemsei.db.base import Base
# This module carries foreign keys into the reporting schema
# (`reporting_datasets`, `report_snapshots`), so those tables must be
# registered before SQLAlchemy tries to resolve them — the same reasoning
# `commercial_models.py` documents for its own tariff/billing foreign keys.
import nemsei.reporting.models  # noqa: E402,F401 - register the referenced tables


PORTFOLIO_STATUSES = ("active", "archived")
PORTFOLIO_SOURCE_KINDS = ("operator", "v1_import")
MEMBERSHIP_SOURCE_KINDS = ("operator", "v1_import", "rule")
# `resolved` has an installation. `unresolved` is a real member whose
# installation is not identified yet. `placeholder` is a slot V1's operators
# created to be filled later and named as such.
RESOLUTION_STATES = ("resolved", "unresolved", "placeholder")
RULE_ATTRIBUTES = (
    "country_code",
    "lifecycle_status",
    "provider_code",
    "contract_type",
    "owner_id",
    "locality",
)
RULE_OPERATORS = ("in", "not_in")


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_portfolios_public_id"),
        UniqueConstraint("slug", name="uq_portfolios_slug"),
        CheckConstraint(f"status IN {PORTFOLIO_STATUSES!r}", name="ck_portfolios_status"),
        CheckConstraint(f"source_kind IN {PORTFOLIO_SOURCE_KINDS!r}", name="ck_portfolios_source_kind"),
        CheckConstraint("(status = 'archived') = (archived_at IS NOT NULL)", name="ck_portfolios_archived"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, default=public_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # "Portfolio por cliente" is this column. Nothing more elaborate is needed:
    # a portfolio scoped to an organization is one owned by that customer.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="operator")
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list["PortfolioMembership"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    rules: Mapped[list["PortfolioRule"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class PortfolioMembership(Base):
    """One member of one portfolio, over one date range."""

    __tablename__ = "portfolio_memberships"
    __table_args__ = (
        CheckConstraint(f"resolution_state IN {RESOLUTION_STATES!r}", name="ck_portfolio_memberships_resolution"),
        CheckConstraint(
            "(resolution_state = 'resolved') = (asset_id IS NOT NULL)",
            name="ck_portfolio_memberships_resolved_asset",
        ),
        CheckConstraint(f"source_kind IN {MEMBERSHIP_SOURCE_KINDS!r}", name="ck_portfolio_memberships_source"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_portfolio_memberships_validity"),
        # A member must be identifiable as something, even without an asset.
        CheckConstraint(
            "asset_id IS NOT NULL OR sub_account IS NOT NULL OR external_name IS NOT NULL",
            name="ck_portfolio_memberships_identity",
        ),
        Index("ix_portfolio_memberships_portfolio", "portfolio_id", "valid_from", "valid_to"),
        Index("ix_portfolio_memberships_asset", "asset_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    # The member's identity inside the portfolio. Every one of V1's 80 members
    # has a distinct sub-account within its own portfolio.
    sub_account: Mapped[str | None] = mapped_column(String(64))
    external_name: Mapped[str | None] = mapped_column(String(255))
    tax_id: Mapped[str | None] = mapped_column(String(64))
    resolution_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unresolved")
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="operator")
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="memberships")


class PortfolioRule(Base):
    """A filter that selects assets into a portfolio.

    Rules select; they never partition. Country and provider live here as
    filters precisely so that they cannot become sub-portfolios.
    """

    __tablename__ = "portfolio_rules"
    __table_args__ = (
        CheckConstraint(f"attribute IN {RULE_ATTRIBUTES!r}", name="ck_portfolio_rules_attribute"),
        CheckConstraint(f"operator IN {RULE_OPERATORS!r}", name="ck_portfolio_rules_operator"),
        Index("ix_portfolio_rules_portfolio", "portfolio_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)
    attribute: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(16), nullable=False, default="in")
    values_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="rules")


class PortfolioSnapshot(Base):
    """The exact membership a report covered, frozen.

    Append-only at the database level. A report issued in March must keep naming
    the plants it actually covered, however the portfolio changes in April.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        CheckConstraint("period_end > period_start", name="ck_portfolio_snapshots_period"),
        UniqueConstraint(
            "portfolio_id", "period_start", "period_end", "membership_digest",
            name="uq_portfolio_snapshots_period_digest",
        ),
        Index("ix_portfolio_snapshots_portfolio", "portfolio_id", "period_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    asset_ids_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    members_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PortfolioDataset(Base):
    """The one source for a portfolio's dashboard, reports and exports."""

    __tablename__ = "portfolio_datasets"
    __table_args__ = (
        CheckConstraint("status IN ('building', 'ready', 'failed')", name="ck_portfolio_datasets_status"),
        CheckConstraint("period_end > period_start", name="ck_portfolio_datasets_period"),
        Index("ix_portfolio_datasets_portfolio", "portfolio_id", "period_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("portfolio_snapshots.id", ondelete="RESTRICT"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="building")
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    totals_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    built_by: Mapped[str] = mapped_column(String(120), nullable=False)

    members: Mapped[list["PortfolioDatasetMember"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class PortfolioDatasetMember(Base):
    """One asset's contribution, and the per-asset dataset it came from."""

    __tablename__ = "portfolio_dataset_members"
    __table_args__ = (
        UniqueConstraint("portfolio_dataset_id", "asset_id", name="uq_portfolio_dataset_members_asset"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_dataset_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_datasets.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    # This column is what makes the aggregate a reuse of the individual
    # reporting rather than a second implementation of it.
    reporting_dataset_id: Mapped[int] = mapped_column(
        ForeignKey("reporting_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    states_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    dataset: Mapped[PortfolioDataset] = relationship(back_populates="members")


RUN_STATUSES = ("generated", "reviewed", "approved")
RUN_MEMBER_STATUSES = ("ready", "blocked")


class PortfolioReportRun(Base):
    """The monthly workflow instance: portfolio, period, and where it stands.

    Validating coverage is a read against data that already exists and leaves
    no row here; a run only starts existing once it is generated. From there it
    is `generated` -> `reviewed` -> `approved`, and a database trigger refuses
    to touch it, or any of its members, once it reaches `approved` — the same
    guarantee `report_snapshots` and `portfolio_snapshots` already give the
    records beneath it.
    """

    __tablename__ = "portfolio_report_runs"
    __table_args__ = (
        CheckConstraint(f"status IN {RUN_STATUSES!r}", name="ck_portfolio_report_runs_status"),
        CheckConstraint("period_end > period_start", name="ck_portfolio_report_runs_period"),
        CheckConstraint(
            "status = 'generated' OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)",
            name="ck_portfolio_report_runs_reviewed",
        ),
        CheckConstraint(
            "status <> 'approved' OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)",
            name="ck_portfolio_report_runs_approved",
        ),
        CheckConstraint(
            "status <> 'generated' OR (reviewed_at IS NULL AND approved_at IS NULL)",
            name="ck_portfolio_report_runs_generated_is_untouched",
        ),
        UniqueConstraint("portfolio_id", "period_start", "period_end", name="uq_portfolio_report_runs_period"),
        Index("ix_portfolio_report_runs_portfolio", "portfolio_id", "period_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="generated")

    portfolio_dataset_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[str] = mapped_column(String(120), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    review_notes: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    members: Mapped[list["PortfolioReportRunMember"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class PortfolioReportRunMember(Base):
    """One member's outcome within a run: reported, or blocked and why."""

    __tablename__ = "portfolio_report_run_members"
    __table_args__ = (
        CheckConstraint(f"status IN {RUN_MEMBER_STATUSES!r}", name="ck_portfolio_report_run_members_status"),
        CheckConstraint(
            "(status = 'ready') = (report_snapshot_id IS NOT NULL)",
            name="ck_portfolio_report_run_members_snapshot",
        ),
        CheckConstraint("status = 'ready' OR reason IS NOT NULL", name="ck_portfolio_report_run_members_reason"),
        UniqueConstraint("run_id", "asset_id", name="uq_portfolio_report_run_members_asset"),
        Index("ix_portfolio_report_run_members_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("portfolio_report_runs.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # Filled through the same assemble_asset_report + snapshot_dataset path an
    # individual report uses on its own — never a second calculation.
    report_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("report_snapshots.id", ondelete="RESTRICT"))

    run: Mapped[PortfolioReportRun] = relationship(back_populates="members")
