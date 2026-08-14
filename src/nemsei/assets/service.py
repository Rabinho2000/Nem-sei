"""Validation and lifecycle rules for physical assets and owners."""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from nemsei.assets.models import ASSET_LIFECYCLE_STATUSES, Asset, AssetAlias, Organization
from nemsei.assets.repository import AssetRepository
from nemsei.shared.clock import utc_now


def normalize_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join("".join(character if character.isalnum() else " " for character in folded).split())


def normalize_tax_id(value: str | None) -> str | None:
    normalized = re.sub(r"\D+", "", value or "")
    return normalized or None


def required_text(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required.")
    return normalized


def create_organization(
    session: Session,
    *,
    display_name: str,
    tax_id: str | None = None,
    review_status: str = "clear",
    review_note: str | None = None,
) -> Organization:
    now = utc_now()
    organization = Organization(
        display_name=required_text(display_name, "Organization name"),
        normalized_tax_id=normalize_tax_id(tax_id),
        review_status=review_status,
        review_note=review_note.strip() if review_note else None,
        created_at=now,
        updated_at=now,
    )
    AssetRepository(session).add(organization)
    session.flush()
    return organization


def create_asset(
    session: Session,
    *,
    canonical_name: str,
    owner_id: int | None = None,
    lifecycle_status: str = "unknown",
    country_code: str | None = None,
    timezone: str = "Europe/Lisbon",
    timezone_source: str = "manual",
    installed_dc_power_kw: Decimal | None = None,
    commissioned_on: date | None = None,
    address: str | None = None,
    locality: str | None = None,
    technical_notes: str | None = None,
    review_status: str = "clear",
    review_note: str | None = None,
) -> Asset:
    name = required_text(canonical_name, "Asset name")
    if lifecycle_status not in ASSET_LIFECYCLE_STATUSES:
        raise ValueError("Invalid asset lifecycle status.")
    repository = AssetRepository(session)
    if owner_id is not None and repository.organization(owner_id) is None:
        raise ValueError("Unknown asset owner.")
    if installed_dc_power_kw is not None and installed_dc_power_kw < 0:
        raise ValueError("Installed power cannot be negative.")
    now = utc_now()
    asset = Asset(
        canonical_name=name,
        normalized_name=normalize_name(name),
        lifecycle_status=lifecycle_status,
        owner_id=owner_id,
        country_code=country_code.strip().upper() if country_code else None,
        timezone=required_text(timezone, "Timezone"),
        timezone_source=required_text(timezone_source, "Timezone source"),
        installed_dc_power_kw=installed_dc_power_kw,
        commissioned_on=commissioned_on,
        address=address.strip() if address else None,
        locality=locality.strip() if locality else None,
        technical_notes=technical_notes.strip() if technical_notes else None,
        review_status=review_status,
        review_note=review_note.strip() if review_note else None,
        created_at=now,
        updated_at=now,
    )
    repository.add(asset)
    session.flush()
    return asset


def add_alias(
    session: Session,
    *,
    asset_id: int,
    alias: str,
    alias_kind: str = "manual",
    source: str = "manual",
    valid_from: date | None = None,
) -> AssetAlias:
    repository = AssetRepository(session)
    if repository.asset(asset_id) is None:
        raise ValueError("Unknown asset.")
    value = required_text(alias, "Alias")
    now = utc_now()
    record = AssetAlias(
        asset_id=asset_id,
        alias=value,
        normalized_alias=normalize_name(value),
        alias_kind=required_text(alias_kind, "Alias kind"),
        source=required_text(source, "Alias source"),
        valid_from=valid_from or now.date(),
        active=True,
        created_at=now,
    )
    repository.add(record)
    session.flush()
    return record
