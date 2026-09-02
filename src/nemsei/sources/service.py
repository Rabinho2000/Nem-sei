"""Validation and deterministic resolution of temporal source policies."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.audit import record_operator_action
from nemsei.providers.models import AssetProviderMapping
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sources.models import SOURCE_USES, AssetSourcePolicy
from nemsei.sources.repository import SourcePolicyRepository


# How many days a production day stays inside the primary's own normal
# reconciliation window before a fallback policy is even considered. Never
# zero: without it, "today" (which the primary's own incremental sync has
# not run yet) would look exactly like "the primary genuinely has nothing
# for this day", and every live, current-day read would redirect to the
# fallback before the primary ever got its own chance to run.
_FALLBACK_GRACE_DAYS = 2


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
    """The mapping this asset/use/day reads from -- the primary, unless it
    has genuinely nothing for this day and a fallback exists to try instead.

    "Nothing" means no `ProductionFact` was ever recorded for the primary's
    mapping on this day -- not that a read failed, not that the value came
    back "missing" (that IS an answer, a real one, and does not redirect
    anywhere). This function has no network/job context to know about a
    failed HTTP call; the durable signal it can see is what got persisted,
    or didn't.

    Scoped to `source_use == "production"` (the only use `ProductionFact`
    covers) and to a day already past `_FALLBACK_GRACE_DAYS` -- so a live,
    current-day incremental sync, whose primary has simply not run yet
    today, is never redirected before it gets its own chance.
    """
    candidates = SourcePolicyRepository(session).active_for(asset_id=asset_id, source_use=source_use, on_date=on_date)
    primaries = [policy for policy in candidates if not policy.is_fallback]
    if not primaries:
        raise ValueError("No primary source policy is valid for this period")
    top_priority = primaries[0].priority
    competing = [policy for policy in primaries if policy.priority == top_priority]
    if len(competing) != 1:
        raise ValueError("Competing primary source policies require reconciliation")
    primary = competing[0]
    if source_use == "production" and _outside_grace_window(on_date) and not _has_recorded_production(session, provider_mapping_id=primary.provider_mapping_id, on_date=on_date):
        fallback = _best_fallback(candidates)
        if fallback is not None:
            return fallback
    return primary


def _outside_grace_window(on_date: date) -> bool:
    return on_date <= datetime.now(timezone.utc).date() - timedelta(days=_FALLBACK_GRACE_DAYS)


def _has_recorded_production(session: Session, *, provider_mapping_id: int, on_date: date) -> bool:
    day_start = datetime.combine(on_date, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    return (
        session.scalar(
            select(ProductionFact.id)
            .where(
                ProductionFact.provider_mapping_id == provider_mapping_id,
                ProductionFact.granularity == "day",
                ProductionFact.metric_kind == "production_energy",
                ProductionFact.period_start >= day_start,
                ProductionFact.period_start < day_end,
            )
            .limit(1)
        )
        is not None
    )


def _best_fallback(candidates: list[AssetSourcePolicy]) -> AssetSourcePolicy | None:
    fallbacks = sorted((policy for policy in candidates if policy.is_fallback), key=lambda policy: (policy.priority, policy.id))
    return fallbacks[0] if fallbacks else None


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
