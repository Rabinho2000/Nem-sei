"""Validation and lifecycle rules for physical assets and owners."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from nemsei.assets.models import (
    ASSET_LIFECYCLE_STATUSES,
    DEVICE_KINDS,
    IMPORT_REVIEW_STATUSES,
    Asset,
    AssetAlias,
    Device,
    Organization,
)
from nemsei.assets.repository import AssetRepository
# identity_decisions.py already reaches for this from inside assets/, so the
# direction is established, not new.
from nemsei.providers.audit import record_operator_action
from nemsei.shared.clock import utc_now


def normalize_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join("".join(character if character.isalnum() else " " for character in folded).split())


def asset_search_clause(search: str):
    """A name/alias/owner match clause, shared by every screen that searches
    assets by free text, so "search for an asset" means one thing everywhere.
    """
    normalized = normalize_name(search)
    if not normalized:
        return None
    pattern = f"%{normalized}%"
    alias_match = exists(
        select(1).where(
            AssetAlias.asset_id == Asset.id,
            AssetAlias.active.is_(True),
            AssetAlias.normalized_alias.ilike(pattern),
        )
    )
    return or_(
        Asset.normalized_name.ilike(pattern),
        func.lower(Organization.display_name).ilike(pattern),
        alias_match,
    )


def normalize_tax_id(value: str | None) -> str | None:
    normalized = re.sub(r"\D+", "", value or "")
    return normalized or None


def normalize_country_code(value: str | None) -> str | None:
    """Normalize a country code without guessing arbitrary legacy values."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) != 2 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("Country code must be exactly two ASCII letters.")
    return normalized.upper()


def normalize_serial_number(value: str | None) -> str | None:
    """Fold a hardware serial for comparison without inventing one."""
    if value is None:
        return None
    folded = "".join(character for character in value.strip().upper() if character.isalnum())
    return folded or None


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
    timezone: str | None = "Europe/Lisbon",
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
        country_code=normalize_country_code(country_code),
        timezone=required_text(timezone, "Timezone") if timezone else None,
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


def _clean(value: str | None) -> str | None:
    """Empty and whitespace-only both mean "not stated", never the empty string."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def update_asset(
    session: Session,
    *,
    asset_id: int,
    canonical_name: str,
    owner_id: int | None,
    lifecycle_status: str,
    country_code: str | None,
    timezone: str | None,
    installed_dc_power_kw: Decimal | None,
    locality: str | None,
    address: str | None,
    technical_notes: str | None,
    commissioned_on: date | None = None,
    review_status: str | None = None,
    review_note: str | None = None,
    actor_username: str | None = None,
) -> Asset:
    """Apply an operator's edit to one asset and record what actually changed.

    `timezone` is deliberately optional. Before Bloco A the web route passed
    `"Europe/Lisbon"` whenever the form omitted the field and this function
    then required it, so any edit stamped a timezone on a plant nobody had
    checked -- and stamped `timezone_source="manual"` alongside, asserting a
    human decision that never happened. Half the fleet has no timezone; a
    missing value has to stay missing.

    `review_status` is the field the detail page could never reach: it was not
    a parameter here at all, so every one of the 266 imported assets was stuck
    on "needs review" no matter what an operator did.
    """
    repository = AssetRepository(session)
    asset = repository.asset(asset_id)
    if asset is None:
        raise ValueError("Unknown asset.")
    if lifecycle_status not in ASSET_LIFECYCLE_STATUSES:
        raise ValueError("Invalid asset lifecycle status.")
    if review_status is not None and review_status not in IMPORT_REVIEW_STATUSES:
        raise ValueError("Invalid asset review status.")
    if owner_id is not None and repository.organization(owner_id) is None:
        raise ValueError("Unknown asset owner.")
    if installed_dc_power_kw is not None and installed_dc_power_kw < 0:
        raise ValueError("Installed power cannot be negative.")

    name = required_text(canonical_name, "Asset name")
    incoming: dict[str, Any] = {
        "canonical_name": name,
        "owner_id": owner_id,
        "lifecycle_status": lifecycle_status,
        "country_code": normalize_country_code(country_code),
        "timezone": _clean(timezone),
        "installed_dc_power_kw": installed_dc_power_kw,
        "commissioned_on": commissioned_on,
        "locality": _clean(locality),
        "address": _clean(address),
        "technical_notes": _clean(technical_notes),
    }
    if review_status is not None:
        incoming["review_status"] = review_status
        incoming["review_note"] = _clean(review_note)

    changed = [field for field, value in incoming.items() if getattr(asset, field) != value]
    if not changed:
        return asset

    for field, value in incoming.items():
        setattr(asset, field, value)
    asset.normalized_name = normalize_name(name)
    # Provenance follows the edit, not the request: only say a human chose the
    # timezone when a human actually changed it.
    if "timezone" in changed:
        asset.timezone_source = "manual"
    asset.updated_at = utc_now()

    if actor_username:
        record_operator_action(
            session,
            actor_username=actor_username,
            action="asset_reviewed" if "review_status" in changed else "asset_updated",
            entity_type="asset",
            entity_id=asset.id,
            metadata={"asset_id": asset.id, "fields_changed": sorted(changed), "review_status": asset.review_status},
        )
    session.flush()
    return asset


_UNSET: Any = object()


def bulk_update_assets(
    session: Session,
    *,
    asset_ids: Sequence[int],
    actor_username: str,
    timezone: str | None = _UNSET,
    lifecycle_status: str | None = _UNSET,
    review_status: str | None = _UNSET,
) -> int:
    """Apply one change to many assets, for the fixes that are fleet-wide.

    132 assets have no timezone and all 266 sit at lifecycle "unknown"; those
    are not 266 visits to a form. A field left at `_UNSET` is untouched, which
    is what separates "clear this value" from "do not consider this field".
    """
    if lifecycle_status is not _UNSET and lifecycle_status not in ASSET_LIFECYCLE_STATUSES:
        raise ValueError("Invalid asset lifecycle status.")
    if review_status is not _UNSET and review_status not in IMPORT_REVIEW_STATUSES:
        raise ValueError("Invalid asset review status.")
    if timezone is not _UNSET and not _clean(timezone):
        raise ValueError("Bulk edit cannot blank a timezone; edit the asset instead.")
    requested = {field for field, value in (("timezone", timezone), ("lifecycle_status", lifecycle_status), ("review_status", review_status)) if value is not _UNSET}
    if not requested:
        raise ValueError("Nothing to change.")

    unique_ids = list(dict.fromkeys(int(value) for value in asset_ids))
    if not unique_ids:
        raise ValueError("Select at least one asset.")

    repository = AssetRepository(session)
    now = utc_now()
    touched = 0
    for asset_id in unique_ids:
        asset = repository.asset(asset_id)
        if asset is None:
            raise ValueError("Unknown asset.")
        changed = []
        if timezone is not _UNSET and asset.timezone != _clean(timezone):
            asset.timezone, asset.timezone_source = _clean(timezone), "manual"
            changed.append("timezone")
        if lifecycle_status is not _UNSET and asset.lifecycle_status != lifecycle_status:
            asset.lifecycle_status = lifecycle_status
            changed.append("lifecycle_status")
        if review_status is not _UNSET and asset.review_status != review_status:
            asset.review_status = review_status
            changed.append("review_status")
        if changed:
            asset.updated_at = now
            touched += 1

    record_operator_action(
        session,
        actor_username=actor_username,
        action="asset_reviewed" if review_status is not _UNSET else "asset_updated",
        entity_type="asset",
        entity_id=None,
        metadata={"fields_changed": sorted(requested), "asset_count": touched},
    )
    session.flush()
    return touched


def create_device(
    session: Session,
    *,
    asset_id: int,
    device_kind: str,
    serial_number: str | None = None,
    label: str | None = None,
    model: str | None = None,
    rated_power_kw: Decimal | None = None,
    lifecycle_status: str = "unknown",
    parent_device_id: int | None = None,
    valid_from: date | None = None,
    review_status: str = "clear",
    review_note: str | None = None,
) -> Device:
    repository = AssetRepository(session)
    if repository.asset(asset_id) is None:
        raise ValueError("Unknown asset.")
    if device_kind not in DEVICE_KINDS:
        raise ValueError("Invalid device kind.")
    if lifecycle_status not in ASSET_LIFECYCLE_STATUSES:
        raise ValueError("Invalid device lifecycle status.")
    if rated_power_kw is not None and rated_power_kw < 0:
        raise ValueError("Rated power cannot be negative.")
    if parent_device_id is not None:
        parent = repository.device(parent_device_id)
        if parent is None or parent.asset_id != asset_id:
            raise ValueError("A parent device must belong to the same asset.")
    normalized_serial = normalize_serial_number(serial_number)
    if normalized_serial and repository.active_serial_claim(asset_id=asset_id, normalized_serial_number=normalized_serial):
        raise ValueError("Device serial number is already claimed for this asset.")
    now = utc_now()
    device = Device(
        asset_id=asset_id,
        parent_device_id=parent_device_id,
        device_kind=device_kind,
        serial_number=serial_number.strip() if serial_number and serial_number.strip() else None,
        normalized_serial_number=normalized_serial,
        label=label.strip() if label else None,
        model=model.strip() if model else None,
        rated_power_kw=rated_power_kw,
        lifecycle_status=lifecycle_status,
        review_status=review_status,
        review_note=review_note.strip() if review_note else None,
        valid_from=valid_from or now.date(),
        created_at=now,
        updated_at=now,
    )
    repository.add(device)
    session.flush()
    return device


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
