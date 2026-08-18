"""Validation and deterministic resolution of temporal source policies."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.providers.audit import record_operator_action
from nemsei.providers.models import AssetProviderMapping
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sources.models import SOURCE_USES, AssetSourcePolicy
from nemsei.sources.repository import SourcePolicyRepository


def create_source_policy(
    session: Session,
    *,
    asset_id: int,
    provider_mapping_id: int,
    source_use: str,
    priority: int,
    valid_from: date,
    valid_to: date | None = None,
    is_fallback: bool = False,
    actor_username: str | None = None,
) -> AssetSourcePolicy:
    if source_use not in SOURCE_USES or priority <= 0:
        raise ValueError("Invalid source policy use or priority")
    if valid_to is not None and valid_to < valid_from:
        raise ValueError("Policy valid_to cannot precede valid_from")
    mapping = session.execute(
        select(AssetProviderMapping).where(AssetProviderMapping.id == provider_mapping_id).with_for_update()
    ).scalar_one_or_none()
    if mapping is None or mapping.asset_id != asset_id:
        raise ValueError("Source policy mapping must belong to the asset")
    if mapping.mapping_status != "active":
        raise ValueError("Source policy requires an approved active mapping.")
    now = utc_now()
    policy = AssetSourcePolicy(
        asset_id=asset_id,
        provider_mapping_id=provider_mapping_id,
        source_use=source_use,
        priority=priority,
        is_fallback=is_fallback,
        valid_from=valid_from,
        valid_to=valid_to,
        created_at=now,
        updated_at=now,
    )
    session.add(policy)
    if actor_username:
        record_operator_action(
            session,
            actor_username=actor_username,
            action="source_policy_created",
            entity_type="asset_source_policy",
            entity_id=None,
            metadata={
                "asset_id": asset_id,
                "mapping_id": provider_mapping_id,
                "source_use": source_use,
                "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat() if valid_to else None,
                "priority": priority,
                "is_fallback": is_fallback,
            },
        )
    return policy


def resolve_source_policy(session: Session, *, asset_id: int, source_use: str, on_date: date) -> AssetSourcePolicy:
    candidates = SourcePolicyRepository(session).active_for(asset_id=asset_id, source_use=source_use, on_date=on_date)
    primaries = [policy for policy in candidates if not policy.is_fallback]
    if not primaries:
        raise ValueError("No primary source policy is valid for this period")
    top_priority = primaries[0].priority
    competing = [policy for policy in primaries if policy.priority == top_priority]
    if len(competing) != 1:
        raise ValueError("Competing primary source policies require reconciliation")
    return competing[0]


def source_policy_date_for_asset(session: Session, *, asset_id: int, at: datetime | None = None) -> date:
    """Resolve a date-only source policy in the asset's explicit local calendar."""
    asset = session.get(Asset, asset_id)
    if asset is None or not asset.timezone:
        raise ValueError("Asset timezone is required to resolve a source policy.")
    try:
        timezone = ZoneInfo(asset.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Asset timezone is invalid.") from exc
    return as_utc(at or utc_now()).astimezone(timezone).date()
