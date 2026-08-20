"""Append-only device status persistence, mirroring `record_production_fact`."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Device
from nemsei.diagnostics.models import AVAILABILITY_STATES, FRESHNESS_STATES, QUALITY_STATES, SOURCE_KINDS, DeviceStatusFact
from nemsei.diagnostics.repository import DeviceStatusRepository
from nemsei.shared.clock import as_utc, utc_now


def record_device_status(
    session: Session,
    *,
    device_id: int,
    asset_id: int,
    source_fact_key: str,
    observed_at: datetime,
    availability_status: str,
    active_power_kw: Decimal | None = None,
    day_energy_kwh: Decimal | None = None,
    source_kind: str = "v1_import",
    # `unknown` matches what every Fatia 1 (`v1_import`) row means by
    # omission: V1 recorded no freshness/quality signal of its own, so a
    # caller that does not pass one is not defaulting to false confidence,
    # it is stating the same absence Fatia 1 already stated structurally.
    freshness: str = "unknown",
    quality: str = "unknown",
    completeness: str = "unknown",
    sync_run_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    # Mirrors `monitoring.service.confirm_current_monitoring`'s flag of the
    # same name. A live poll with no independent provider timestamp
    # (freshness "unknown") always writes `observed_at=ingested_at`, which
    # differs on every call by construction; without this flag an unchanged
    # reading would mint a new revision at every single poll forever, noise
    # indistinguishable from a real change. Set only when the caller cannot
    # otherwise vouch for `observed_at` being real evidence of a new instant.
    deduplicate_observed_at: bool = False,
) -> tuple[DeviceStatusFact, bool]:
    if availability_status not in AVAILABILITY_STATES:
        raise ValueError("Invalid device availability status")
    if source_kind not in SOURCE_KINDS:
        raise ValueError("Invalid device status source kind")
    if freshness not in FRESHNESS_STATES:
        raise ValueError("Invalid device status freshness")
    if quality not in QUALITY_STATES:
        raise ValueError("Invalid device status quality")
    if completeness not in QUALITY_STATES:
        raise ValueError("Invalid device status completeness")
    device = session.get(Device, device_id)
    if device is None or device.asset_id != asset_id:
        raise ValueError("Device status fact must belong to its own asset")
    key = source_fact_key.strip()
    if not key:
        raise ValueError("Device status fact key is required")

    existing = DeviceStatusRepository(session).latest_fact(device_id=device_id, source_key=key)
    normalized = (
        None if deduplicate_observed_at else as_utc(observed_at), availability_status, active_power_kw, day_energy_kwh,
        source_kind, freshness, quality, completeness, metadata or {},
    )
    if existing and normalized == (
        None if deduplicate_observed_at else as_utc(existing.observed_at), existing.availability_status,
        existing.active_power_kw, existing.day_energy_kwh, existing.source_kind,
        existing.freshness, existing.quality, existing.completeness, existing.metadata_json,
    ):
        return existing, False

    fact = DeviceStatusFact(
        device_id=device_id,
        asset_id=asset_id,
        source_fact_key=key,
        source_revision=(existing.source_revision + 1) if existing else 1,
        supersedes_fact_id=existing.id if existing else None,
        observed_at=as_utc(observed_at),
        ingested_at=utc_now(),
        availability_status=availability_status,
        active_power_kw=active_power_kw,
        day_energy_kwh=day_energy_kwh,
        source_kind=source_kind,
        freshness=freshness,
        quality=quality,
        completeness=completeness,
        sync_run_id=sync_run_id,
        metadata_json=metadata or {},
    )
    session.add(fact)
    session.flush()
    return fact, True


def current_device_status(session: Session, *, asset_id: int) -> list[dict[str, Any]]:
    """One row per device: its most recent reading, or nothing if it never had one.

    Every device the asset owns is listed, including a device with zero facts —
    "no reading exists" is a diagnostic answer in itself, not something to
    silently omit from the list.
    """
    devices = session.scalars(select(Device).where(Device.asset_id == asset_id).order_by(Device.id)).all()
    latest = {fact.device_id: fact for fact in DeviceStatusRepository(session).current_status_for_asset(asset_id=asset_id)}
    rows: list[dict[str, Any]] = []
    for device in devices:
        fact = latest.get(device.id)
        rows.append(
            {
                "device_id": device.id,
                "label": device.label or device.serial_number or f"Device #{device.id}",
                "device_kind": device.device_kind,
                "model": device.model,
                "rated_power_kw": device.rated_power_kw,
                "observed_at": fact.observed_at if fact else None,
                "availability_status": fact.availability_status if fact else "unknown",
                "active_power_kw": fact.active_power_kw if fact else None,
                "day_energy_kwh": fact.day_energy_kwh if fact else None,
                "freshness": fact.freshness if fact else "unknown",
                "source_kind": fact.source_kind if fact else None,
                "has_reading": fact is not None,
            }
        )
    return rows
