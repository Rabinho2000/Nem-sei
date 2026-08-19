"""Resolve a portfolio's membership, and freeze it.

A portfolio's members come from two places: rows an operator (or the V1 import)
recorded, and rules that select assets by attribute. Both are resolved as of a
date, because "who was in this portfolio in March" is the question a March
report has to answer in December.

Rules select assets; they never partition a portfolio. Country, region and
provider are filters here, which is what stops them becoming sub-portfolios.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.assets.service import asset_search_clause, normalize_tax_id
from nemsei.portfolios.models import (
    RULE_ATTRIBUTES,
    RULE_OPERATORS,
    Portfolio,
    PortfolioMembership,
    PortfolioRule,
    PortfolioSnapshot,
)
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.shared.clock import utc_now


@dataclass(frozen=True)
class ResolvedMember:
    """One member of a portfolio as of a date."""

    asset_id: int | None
    sub_account: str | None
    external_name: str | None
    tax_id: str | None
    resolution_state: str
    origin: str  # "membership" or "rule"
    membership_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "sub_account": self.sub_account,
            "external_name": self.external_name,
            "tax_id": self.tax_id,
            "resolution_state": self.resolution_state,
            "origin": self.origin,
            "membership_id": self.membership_id,
        }


def slugify(value: str) -> str:
    """A stable, readable identifier. Accents are folded, not dropped."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    return slug or "portfolio"


def _covering(moment: date):
    return (PortfolioMembership.valid_from <= moment) & or_(
        PortfolioMembership.valid_to.is_(None), PortfolioMembership.valid_to > moment
    )


def assets_matching_rules(session: Session, rules: list[PortfolioRule]) -> list[int]:
    """Asset ids selected by a portfolio's rules.

    Rules combine with AND: every rule must accept an asset. An `in` rule with
    no values selects nothing rather than everything, because an empty filter
    that matches the whole estate is how a portfolio silently becomes the
    entire company.
    """
    if not rules:
        return []
    statement = select(Asset.id)
    provider_rules = [rule for rule in rules if rule.attribute == "provider_code"]
    for rule in rules:
        values = list(rule.values_json or [])
        if not values:
            return []
        if rule.attribute == "provider_code":
            continue
        column = getattr(Asset, rule.attribute)
        statement = statement.where(column.in_(values) if rule.operator == "in" else column.notin_(values))
    for rule in provider_rules:
        values = list(rule.values_json or [])
        claimed = (
            select(AssetProviderMapping.asset_id)
            .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
            .where(
                ProviderConnection.provider_code.in_(values),
                AssetProviderMapping.mapping_status == "active",
            )
        )
        statement = statement.where(
            Asset.id.in_(claimed) if rule.operator == "in" else Asset.id.notin_(claimed)
        )
    return sorted(session.scalars(statement).all())


def resolve_members(session: Session, *, portfolio_id: int, on: date) -> list[ResolvedMember]:
    """Every member of a portfolio on a date, from rows and from rules.

    An asset selected by a rule that is already an explicit member appears once.
    An explicit row wins, because someone stated it deliberately.
    """
    portfolio = session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Unknown portfolio.")

    memberships = session.scalars(
        select(PortfolioMembership)
        .where(PortfolioMembership.portfolio_id == portfolio_id, _covering(on))
        .order_by(PortfolioMembership.sub_account, PortfolioMembership.id)
    ).all()

    members = [
        ResolvedMember(
            asset_id=row.asset_id,
            sub_account=row.sub_account,
            external_name=row.external_name,
            tax_id=row.tax_id,
            resolution_state=row.resolution_state,
            origin="membership",
            membership_id=row.id,
        )
        for row in memberships
    ]
    explicit = {member.asset_id for member in members if member.asset_id is not None}

    rules = session.scalars(select(PortfolioRule).where(PortfolioRule.portfolio_id == portfolio_id)).all()
    for asset_id in assets_matching_rules(session, list(rules)):
        if asset_id in explicit:
            continue
        asset = session.get(Asset, asset_id)
        members.append(
            ResolvedMember(
                asset_id=asset_id,
                sub_account=None,
                external_name=asset.canonical_name if asset else None,
                tax_id=None,
                resolution_state="resolved",
                origin="rule",
            )
        )
    return members


def membership_digest(members: list[ResolvedMember]) -> str:
    """Identity of a membership list: the same members give the same digest."""
    # A member without an asset sorts before one with an asset rather than
    # blowing up on None < int, and the ordering is stable either way so the
    # same membership always produces the same digest.
    payload = sorted(
        (
            member.asset_id is not None,
            member.asset_id or 0,
            member.sub_account or "",
            member.external_name or "",
            member.resolution_state,
        )
        for member in members
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_portfolio(
    session: Session,
    *,
    name: str,
    created_by: str,
    description: str | None = None,
    owner_id: int | None = None,
    source_kind: str = "operator",
    provenance: dict[str, Any] | None = None,
) -> Portfolio:
    label = name.strip()
    actor = created_by.strip()
    if not label:
        raise ValueError("A portfolio needs a name.")
    if not actor:
        raise ValueError("A portfolio must record who created it.")
    now = utc_now()
    portfolio = Portfolio(
        name=label,
        slug=slugify(label),
        description=(description or "").strip() or None,
        owner_id=owner_id,
        status="active",
        source_kind=source_kind,
        provenance_json=provenance or {},
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(portfolio)
    session.flush()
    return portfolio


def add_member(
    session: Session,
    *,
    portfolio_id: int,
    valid_from: date,
    created_by: str,
    asset_id: int | None = None,
    sub_account: str | None = None,
    external_name: str | None = None,
    tax_id: str | None = None,
    resolution_state: str | None = None,
    valid_to: date | None = None,
    source_kind: str = "operator",
    provenance: dict[str, Any] | None = None,
    notes: str | None = None,
) -> PortfolioMembership:
    """Record a member. A member without an asset is still a member."""
    actor = created_by.strip()
    if not actor:
        raise ValueError("A membership must record who added it.")
    if session.get(Portfolio, portfolio_id) is None:
        raise ValueError("Unknown portfolio.")
    state = resolution_state or ("resolved" if asset_id is not None else "unresolved")
    now = utc_now()
    membership = PortfolioMembership(
        portfolio_id=portfolio_id,
        asset_id=asset_id,
        sub_account=sub_account,
        external_name=external_name,
        tax_id=tax_id,
        resolution_state=state,
        valid_from=valid_from,
        valid_to=valid_to,
        source_kind=source_kind,
        provenance_json=provenance or {},
        notes=notes,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(membership)
    session.flush()
    return membership


def end_membership(session: Session, *, membership_id: int, on: date) -> PortfolioMembership:
    """Close a membership from a date, without deleting the history.

    Only an **open** membership can be ended this way. Rewriting an already-set
    `valid_to` would silently replace one operator's decision with another's and
    leave no trace that it happened — the same reasoning `close_open_row` uses
    for tariffs and billing configuration.
    """
    membership = session.get(PortfolioMembership, membership_id)
    if membership is None:
        raise ValueError("Unknown membership.")
    if membership.valid_to is not None:
        raise ValueError(
            f"This membership already ended on {membership.valid_to.isoformat()}; "
            "record a new membership instead of rewriting the old one's end date."
        )
    if on <= membership.valid_from:
        raise ValueError("A membership cannot end on or before the day it started.")
    membership.valid_to = on
    membership.updated_at = utc_now()
    session.flush()
    return membership


def resolve_member_to_asset(
    session: Session, *, membership_id: int, asset_id: int, resolved_by: str
) -> PortfolioMembership:
    """Link an unresolved member to an installation, deliberately.

    This is the only path from `unresolved` to `resolved`, and it takes an
    actor. Nothing in this module guesses: a name or a NIF may *suggest* an
    asset, and suggesting is where automation stops.
    """
    membership = session.get(PortfolioMembership, membership_id)
    if membership is None:
        raise ValueError("Unknown membership.")
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ValueError("Unknown asset.")
    # Read before the flush that might fail: a rollback expires every attribute
    # on every object touched in this transaction, and re-fetching one to build
    # an error message is exactly the kind of query a failed transaction can't
    # serve.
    asset_name = asset.canonical_name
    actor = resolved_by.strip()
    if not actor:
        raise ValueError("Resolving a member must record who did it.")
    membership.asset_id = asset_id
    membership.resolution_state = "resolved"
    membership.provenance_json = {
        **(membership.provenance_json or {}),
        "resolved_by": actor,
        "resolved_at": utc_now().isoformat(),
    }
    membership.updated_at = utc_now()
    # The database is the source of truth on whether this overlaps another
    # membership of the same asset — the exclusion constraint already encodes
    # the date-range arithmetic correctly. A nested transaction lets that
    # answer surface as a clear message instead of a raw IntegrityError
    # aborting whatever the caller's outer transaction was doing.
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as error:
        if "ex_portfolio_memberships_no_overlap" not in str(error.orig):
            raise
        raise ValueError(
            f"{asset_name} is already a member of this portfolio for an overlapping period; "
            "end that membership first if this one should replace it."
        ) from error
    return membership


def unresolved_members(session: Session, *, portfolio_id: int | None = None) -> list[PortfolioMembership]:
    """Every open member that has no installation yet, across one portfolio or all.

    Only open memberships are listed: a member whose row already ended is
    history, not something to review.
    """
    statement = select(PortfolioMembership).where(
        PortfolioMembership.resolution_state != "resolved",
        PortfolioMembership.valid_to.is_(None),
    )
    if portfolio_id is not None:
        statement = statement.where(PortfolioMembership.portfolio_id == portfolio_id)
    return list(
        session.scalars(statement.order_by(PortfolioMembership.portfolio_id, PortfolioMembership.sub_account)).all()
    )


def search_asset_candidates(session: Session, query: str, *, limit: int = 15) -> list[Asset]:
    """Assets whose name, alias or owner matches free text.

    A search result is only ever a suggestion. Nothing here writes anything;
    `resolve_member_to_asset` is still the one place a link is actually made,
    and it always takes an explicit asset id from whoever is looking.
    """
    clause = asset_search_clause(query)
    if clause is None:
        return []
    statement = (
        select(Asset)
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .where(clause)
        .order_by(Asset.canonical_name)
        .limit(limit)
    )
    return list(session.scalars(statement).all())


def suggest_candidates_for_member(session: Session, membership: PortfolioMembership) -> dict[str, Any]:
    """Everything that might help an operator resolve one member, and nothing that decides for them.

    A NIF that matches a V2 organization exactly names a *customer*, not an
    installation — an organization can own several plants, or none imported
    yet — so it is offered as context, never as a one-click resolution. A name
    search over assets is the closest thing to an actual candidate list, and it
    is still just a list to choose from.
    """
    organization = None
    tax_id = normalize_tax_id(membership.tax_id) if membership.tax_id else None
    if tax_id:
        organization = session.scalar(select(Organization).where(Organization.normalized_tax_id == tax_id))
    name_candidates = (
        search_asset_candidates(session, membership.external_name, limit=8) if membership.external_name else []
    )
    organization_assets = (
        list(session.scalars(select(Asset).where(Asset.owner_id == organization.id).order_by(Asset.canonical_name)))
        if organization is not None
        else []
    )
    return {
        "organization": organization,
        "organization_assets": organization_assets,
        "name_candidates": [asset for asset in name_candidates if asset not in organization_assets],
    }


def add_rule(
    session: Session,
    *,
    portfolio_id: int,
    attribute: str,
    values: list[Any],
    created_by: str,
    operator: str = "in",
) -> PortfolioRule:
    actor = created_by.strip()
    if not actor:
        raise ValueError("A rule must record who added it.")
    if session.get(Portfolio, portfolio_id) is None:
        raise ValueError("Unknown portfolio.")
    if attribute not in RULE_ATTRIBUTES:
        raise ValueError(f"Unknown rule attribute: {attribute}.")
    if operator not in RULE_OPERATORS:
        raise ValueError(f"Unknown rule operator: {operator}.")
    if not values:
        raise ValueError("A rule with no values would select nothing; state its values.")
    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        attribute=attribute,
        operator=operator,
        values_json=list(values),
        created_by=actor,
        created_at=utc_now(),
    )
    session.add(rule)
    session.flush()
    return rule


def freeze_snapshot(
    session: Session,
    *,
    portfolio_id: int,
    period_start: date,
    period_end: date,
    created_by: str,
) -> PortfolioSnapshot:
    """Freeze the exact membership covering a period.

    Membership is resolved as of the period's **first day**: a report for March
    covers the portfolio as March began. Re-freezing an unchanged membership
    returns the snapshot that already exists rather than creating a second one.
    """
    if period_end <= period_start:
        raise ValueError("A period must end after it starts.")
    actor = created_by.strip()
    if not actor:
        raise ValueError("A snapshot must record who created it.")
    members = resolve_members(session, portfolio_id=portfolio_id, on=period_start)
    digest = membership_digest(members)
    existing = session.scalar(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.portfolio_id == portfolio_id,
            PortfolioSnapshot.period_start == period_start,
            PortfolioSnapshot.period_end == period_end,
            PortfolioSnapshot.membership_digest == digest,
        )
    )
    if existing is not None:
        return existing
    snapshot = PortfolioSnapshot(
        portfolio_id=portfolio_id,
        period_start=period_start,
        period_end=period_end,
        asset_ids_json=sorted(m.asset_id for m in members if m.asset_id is not None),
        members_json=[member.as_dict() for member in members],
        membership_digest=digest,
        created_by=actor,
        created_at=utc_now(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot
