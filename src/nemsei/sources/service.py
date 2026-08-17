"""Validation and deterministic resolution of temporal source policies."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from nemsei.providers.models import AssetProviderMapping
from nemsei.shared.clock import utc_now
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
) -> AssetSourcePolicy:
    if source_use not in SOURCE_USES or priority <= 0:
        raise ValueError("Invalid source policy use or priority")
    if valid_to is not None and valid_to < valid_from:
        raise ValueError("Policy valid_to cannot precede valid_from")
    mapping = session.get(AssetProviderMapping, provider_mapping_id)
    if mapping is None or mapping.asset_id != asset_id:
        raise ValueError("Source policy mapping must belong to the asset")
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
