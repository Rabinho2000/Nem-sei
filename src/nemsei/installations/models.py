"""The physical/operational site an Asset lives at.

Three separate questions were being answered by two entities, and the extra
question had nowhere to live:

    where is this, physically, and who operates it       -- Installation
    what is the canonical technical plant, electrically   -- Asset
    how does one provider see that plant                  -- AssetProviderMapping

`AssetProviderMapping` already resolves "one plant, several provider
identities" -- that was never the gap. The gap is that `Asset` was also
carrying the *site* -- its address, its coordinates, eventually its
contracts, its work orders, its visits -- with no way to say "these two
technical plants share one roof" without inventing a second central. A site
with two inverters from two different install phases, sold as one contract,
is one `Installation` with two `Asset` rows.

Today the backfill (`installations/service.py`) makes this 267 Installations
for 267 Assets, one each -- and that is fine. The point is not that today's
data needs the split; the point is that the split makes tomorrow's
many-Assets-one-Installation case a normal row instead of a schema change.

What moves here, conceptually, over time: contracts, work orders, visits,
timeline, module groups, physical location and coordinates, operational
contacts. What stays on `Asset`/`Device`: provider mappings, production
facts, device status facts, devices, and anything that is evidence about one
technical plant rather than about the site it stands on. Incidents stay
anchored on `Asset`/`Device` -- the technical origin of a fault matters, and
this milestone does not touch the diagnostics engine's foreign keys -- but
must stay navigable and aggregable by Installation, which a plain join
through `Asset.installation_id` already gives for free.

Nothing here touches `production_facts`, `device_status_facts`,
`asset_provider_mappings`, or any ingestion path. Those keep pointing at
`Asset`, unmodified.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


def public_id() -> str:
    return str(uuid.uuid4())


# Where a site's coordinates came from, and how much to trust them. V1's own
# words: `suspect` means geocoded from a postal address and never checked,
# which can land in the middle of a municipality rather than on the roof.
COORDINATE_SOURCES = ("google_mymaps", "openrouteservice", "manual", "operator", "provider")
COORDINATE_CONFIDENCES = ("ok", "suspect", "manual")


class Installation(Base):
    """One physical, operational site. Belongs to an `Organization`; carries
    one or more `Asset` rows."""

    __tablename__ = "installations"
    __table_args__ = (
        # Coordinates and their provenance travel together, for the same
        # reason `Asset` refuses one without the other everywhere else in
        # this schema: a number with no recorded origin cannot later be
        # argued with, which is how V1's `suspect` pairs would have become
        # indistinguishable from its traced ones.
        CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR coordinates_source IS NOT NULL",
            name="ck_installations_coordinates_provenance",
        ),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="ck_installations_coordinates_pair"),
        CheckConstraint(
            f"coordinates_source IS NULL OR coordinates_source IN {COORDINATE_SOURCES!r}",
            name="ck_installations_coordinates_source",
        ),
        CheckConstraint(
            f"coordinates_confidence IS NULL OR coordinates_confidence IN {COORDINATE_CONFIDENCES!r}",
            name="ck_installations_coordinates_confidence",
        ),
        Index("ix_installations_organization", "organization_id"),
        Index(
            "ix_installations_coordinates",
            "latitude",
            "longitude",
            postgresql_where=text("latitude IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=public_id)
    # No relationship object to `Organization` on purpose -- `Installation` is
    # a foundational module now, and the FK-by-table-name is enough for every
    # query written against it so far. Add the relationship the day a query
    # actually needs `installation.organization` rather than a join.
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2))
    locality: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(64))
    timezone_source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    # The canonical source for "when is the sun up here" --
    # `monitoring.production_window` takes these as plain arguments and does
    # not care which table they came from.
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    coordinates_source: Mapped[str | None] = mapped_column(String(32))
    coordinates_confidence: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Who Telegram should say to call. See `installations/contacts.py` and the
# Telegram redesign plan (§8): most installations have never had one of these
# recorded, and that absence has to render as "não registado", never as an
# invented number -- so this table is deliberately allowed to have zero rows
# for a real installation.
CONTACT_TYPES = ("client", "facility_manager", "local_maintenance", "security", "owner", "other")


class InstallationContact(Base):
    """One person to call about one installation. An installation can have
    several; `is_primary` is a hint for which to lead with, not the only one
    that exists."""

    __tablename__ = "installation_contacts"
    __table_args__ = (
        CheckConstraint(f"contact_type IN {CONTACT_TYPES!r}", name="ck_installation_contacts_type"),
        # Known-but-unreachable ("we have a name, nobody wrote down how to
        # reach them") is a real, visible gap -- not something this table
        # should silently allow to look like a usable contact.
        CheckConstraint("phone IS NOT NULL OR email IS NOT NULL", name="ck_installation_contacts_reachable"),
        Index("ix_installation_contacts_installation", "installation_id", "is_primary"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[int] = mapped_column(ForeignKey("installations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    contact_type: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    is_primary: Mapped[bool] = mapped_column(nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
