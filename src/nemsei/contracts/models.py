"""O&M service contracts: which installations Solcor actually operates.

V1 answered this question with five columns spread across two tables, and one
of them was not data at all. `assets.active_contract` was *derived*:
`derive_active_contract` returns "yes" when `end_contract >= today`, and a
nightly `sync_all_contract_statuses` pass kept the stored copy from drifting.
Checked against the frozen V1 snapshot, the derivation reproduces all 267 rows
exactly -- the 91 installations carrying an end date yield precisely the 83
"yes" and 8 "no" V1 holds, and the single scoped installation with no dates at
all (V1 asset 2100) falls through to its stored value.

So V2 stores the *period* and derives the state on read. Nothing to schedule,
nothing to drift, and a contract that lapses at midnight is lapsed at midnight.

One row is one engagement over one date range. Renewal is a new row, never an
edit, which is what gives this table the history V1 never had: V1's
`om_contracts` is `UNIQUE (asset_id)`, so renewing overwrote the terms that
were true last year.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nemsei.assets.models import IMPORT_REVIEW_STATUSES, Asset, public_id
from nemsei.db.base import Base


# One value today. The column exists so that a monitoring-only engagement or a
# warranty period is a new row rather than a new table -- the same reasoning
# `DEVICE_KINDS` records for devices.
SERVICE_KINDS = ("om",)
CONTRACT_SOURCE_KINDS = ("v1_import", "operator")
# V1 offered a renewal follow-up field and populated it zero times in seven
# rows. The vocabulary is V2's own because there is no V1 usage to preserve.
RENEWAL_STATUSES = ("not_started", "in_contact", "renewed", "lost")

# Derived on read, never stored -- see the module docstring.
#   active   -- a contract covers the date asked about
#   expired  -- the most recent contract ended before it
#   undated  -- in scope, but nobody wrote down when it runs
#   none     -- no O&M engagement was ever recorded
OM_STATUSES = ("active", "expired", "undated", "none")


class AssetServiceContract(Base):
    """One O&M engagement, for one installation, over one date range."""

    __tablename__ = "asset_service_contracts"
    __table_args__ = (
        CheckConstraint(f"service_kind IN {SERVICE_KINDS!r}", name="ck_asset_service_contracts_kind"),
        CheckConstraint(f"source_kind IN {CONTRACT_SOURCE_KINDS!r}", name="ck_asset_service_contracts_source_kind"),
        CheckConstraint(f"review_status IN {IMPORT_REVIEW_STATUSES!r}", name="ck_asset_service_contracts_review_status"),
        CheckConstraint(
            f"renewal_status IS NULL OR renewal_status IN {RENEWAL_STATUSES!r}",
            name="ck_asset_service_contracts_renewal_status",
        ),
        # A NULL bound is unbounded, not invalid: "we operate this plant but
        # nobody recorded since when" is a real state for two installations in
        # V1, and inventing a start date to satisfy a constraint would be
        # inventing evidence. Those rows carry `review_status='needs_review'`
        # so the gap is visible work rather than a silent guess.
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
            name="ck_asset_service_contracts_validity",
        ),
        CheckConstraint(
            "annual_value_eur IS NULL OR annual_value_eur >= 0",
            name="ck_asset_service_contracts_annual_value",
        ),
        Index("ix_asset_service_contracts_validity", "asset_id", "valid_from", "valid_to"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=public_id)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    service_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="om")

    # Exclusive upper bound, like every other temporal window in this schema.
    # V1's `end_contract` is *inclusive* -- `derive_active_contract` compares
    # `end >= today` -- so the importer adds one day, exactly as the tariff
    # importer already does for `asset_tariffs.valid_to`.
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)

    annual_value_eur: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    renewal_status: Mapped[str | None] = mapped_column(String(32))
    last_contact_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="clear")
    review_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    asset: Mapped[Asset] = relationship()

    def covers(self, on: date) -> bool:
        """Whether this engagement is in force on `on`. NULL bounds are open."""
        if self.valid_from is not None and on < self.valid_from:
            return False
        if self.valid_to is not None and on >= self.valid_to:
            return False
        return True
