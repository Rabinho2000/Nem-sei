"""Reading and writing service engagements, and the state derived from them.

Every screen that asks "does this installation have O&M?" asks it here, so the
question has exactly one answer in the product. `models.py` explains why the
answer is derived rather than stored.

An installation can hold several engagements of different kinds -- O&M and
ESCO are not alternatives. `om_status_map` and `esco_status_map` are thin
wrappers over one `_status_map`, each scoped to its own `service_kind`,
because before ESCO existed there was only ever one kind and nothing needed
to filter by it. Reading unscoped after adding a second kind would count an
ESCO period as O&M coverage; writing unscoped would let recording an ESCO
engagement silently close an unrelated open O&M one. `_close_open_contracts`
carries the same scoping for the same reason.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.contracts.models import (
    CONTRACT_SOURCE_KINDS,
    RENEWAL_STATUSES,
    SERVICE_KINDS,
    AssetServiceContract,
)
from nemsei.shared.clock import utc_now


# How far ahead a renewal stops being a diary note and starts being work. The
# buckets are V1's own (`routes/settings.py` used expired / 0-30 / 31-90 / this
# year); V2 widens the near band to 90 days because the renewals screen is read
# weekly at most, and splits the rest by a rolling year rather than by calendar
# year -- a contract ending on 3 January is not less urgent than one ending on
# 28 December just because a year boundary sits between them.
NEAR_EXPIRY_DAYS = 90
EXPIRY_BUCKETS = ("expired", "within_90_days", "within_a_year", "beyond_a_year", "undated")


def today() -> date:
    return utc_now().date()


def contracts_for(
    session: Session, *, asset_id: int, service_kind: str | None = None
) -> list[AssetServiceContract]:
    """Every recorded engagement for one installation, newest window first.

    `service_kind=None` returns every kind mixed together -- correct for a
    full audit trail, wrong for anything that means "the O&M history" or "the
    ESCO history" specifically. Callers building one of those must pass the
    kind; `asset_contract_panel` in `web/contract_queries.py` is the example
    this distinction was written for.
    """
    statement = select(AssetServiceContract).where(AssetServiceContract.asset_id == asset_id)
    if service_kind is not None:
        statement = statement.where(AssetServiceContract.service_kind == service_kind)
    rows = session.scalars(statement).all()
    return sorted(rows, key=_recency_key, reverse=True)


def _recency_key(contract: AssetServiceContract) -> tuple[date, date]:
    # `date.min` sorts an unknown bound to the far past, which keeps a dated
    # row ahead of an undated one instead of letting NULL decide the order.
    return (contract.valid_to or date.max, contract.valid_from or date.min)


def current_contract(
    session: Session, *, asset_id: int, service_kind: str = "om", on: date | None = None
) -> AssetServiceContract | None:
    """The engagement of one kind in force on `on`, if any."""
    moment = on or today()
    for contract in contracts_for(session, asset_id=asset_id, service_kind=service_kind):
        if contract.covers(moment):
            return contract
    return None


def om_status(session: Session, *, asset_id: int, on: date | None = None) -> str:
    return om_status_map(session, asset_ids=[asset_id], on=on)[asset_id]["status"]


def om_status_map(
    session: Session, *, asset_ids: Sequence[int] | None = None, on: date | None = None
) -> dict[int, dict[str, Any]]:
    """Derived O&M state per installation. See `_status_map`."""
    return _status_map(session, service_kind="om", asset_ids=asset_ids, on=on)


def esco_status_map(
    session: Session, *, asset_ids: Sequence[int] | None = None, on: date | None = None
) -> dict[int, dict[str, Any]]:
    """Derived ESCO state per installation. See `_status_map`.

    Structurally identical to `om_status_map` -- the derivation in
    `_state_for` (active/expired/undated/none from a period, never stored)
    has nothing O&M-specific in it. Only the `service_kind` filter differs.
    """
    return _status_map(session, service_kind="esco", asset_ids=asset_ids, on=on)


def _status_map(
    session: Session, *, service_kind: str, asset_ids: Sequence[int] | None = None, on: date | None = None
) -> dict[int, dict[str, Any]]:
    """Derived state per installation for one engagement kind, in one query.

    Returns an entry for every requested asset, including the ones with no
    engagement at all -- a caller rendering a table must not have to decide
    what a missing key means.
    """
    moment = on or today()
    statement = select(AssetServiceContract).where(AssetServiceContract.service_kind == service_kind)
    if asset_ids is not None:
        if not asset_ids:
            return {}
        statement = statement.where(AssetServiceContract.asset_id.in_(list(asset_ids)))
    by_asset: dict[int, list[AssetServiceContract]] = {}
    for contract in session.scalars(statement):
        by_asset.setdefault(contract.asset_id, []).append(contract)

    known = list(asset_ids) if asset_ids is not None else list(by_asset)
    result: dict[int, dict[str, Any]] = {}
    for asset_id in known:
        result[asset_id] = _state_for(by_asset.get(asset_id, []), on=moment)
    return result


def _state_for(contracts: list[AssetServiceContract], *, on: date) -> dict[str, Any]:
    if not contracts:
        return {
            "status": "none",
            "contract": None,
            "valid_from": None,
            "valid_to": None,
            "days_to_expiry": None,
            "bucket": None,
        }
    ordered = sorted(contracts, key=_recency_key, reverse=True)
    live = next((contract for contract in ordered if contract.covers(on)), None)
    contract = live or ordered[0]
    if live is None:
        status = "expired"
    elif live.valid_from is None and live.valid_to is None:
        # In scope, but the engagement carries no dates at all: saying "active
        # until further notice" would be a stronger claim than the evidence.
        status = "undated"
    else:
        status = "active"
    return {
        "status": status,
        "contract": contract,
        "valid_from": contract.valid_from,
        "valid_to": contract.valid_to,
        "days_to_expiry": _days_to_expiry(contract.valid_to, on=on),
        "bucket": expiry_bucket(contract.valid_to, on=on),
    }


def _days_to_expiry(valid_to: date | None, *, on: date) -> int | None:
    if valid_to is None:
        return None
    # `valid_to` is exclusive, so the last day covered is the day before it.
    return (valid_to - on).days - 1


def expiry_bucket(valid_to: date | None, *, on: date | None = None) -> str:
    moment = on or today()
    if valid_to is None:
        return "undated"
    remaining = _days_to_expiry(valid_to, on=moment)
    assert remaining is not None  # valid_to is not None on this path
    if remaining < 0:
        return "expired"
    if remaining <= NEAR_EXPIRY_DAYS:
        return "within_90_days"
    if remaining <= 365:
        return "within_a_year"
    return "beyond_a_year"


def assets_in_om_scope(session: Session, *, on: date | None = None) -> set[int]:
    """Installations Solcor operates: any recorded engagement, live or lapsed.

    This is V1's `maintenance` flag. A lapsed contract does not remove an
    installation from the O&M portfolio -- it makes it a renewal.
    """
    return {
        asset_id
        for asset_id, state in om_status_map(session, on=on).items()
        if state["status"] != "none"
    }


def assets_with_active_om(session: Session, *, on: date | None = None) -> set[int]:
    """Installations under a contract in force -- V1's `active_contract`.

    `undated` counts as in force: those installations are operated, and the
    only thing missing is paperwork. Excluding them would silence a plant for
    a filing gap.
    """
    return {
        asset_id
        for asset_id, state in om_status_map(session, on=on).items()
        if state["status"] in {"active", "undated"}
    }


def overview(session: Session, *, on: date | None = None) -> dict[str, Any]:
    """Portfolio-level O&M coverage, for the operational panel."""
    moment = on or today()
    states = om_status_map(session, on=moment)
    total_assets = int(session.scalar(select(func.count(Asset.id))) or 0)
    buckets = {bucket: 0 for bucket in EXPIRY_BUCKETS}
    active = expired = undated = 0
    for state in states.values():
        if state["status"] == "active":
            active += 1
        elif state["status"] == "undated":
            undated += 1
        elif state["status"] == "expired":
            expired += 1
        bucket = state["bucket"]
        if bucket is not None:
            buckets[bucket] += 1
    return {
        "in_scope": len(states),
        "active": active,
        "expired": expired,
        "undated": undated,
        "buckets": buckets,
        "as_of": moment,
        "total_assets": total_assets,
    }


def set_service_contract(
    session: Session,
    *,
    asset_id: int,
    created_by: str,
    valid_from: date | None,
    valid_to: date | None = None,
    service_kind: str = "om",
    annual_value_eur: Decimal | None = None,
    renewal_status: str | None = None,
    last_contact_on: date | None = None,
    notes: str | None = None,
    source_kind: str = "operator",
    provenance: dict[str, Any] | None = None,
    review_status: str = "clear",
    review_note: str | None = None,
) -> AssetServiceContract:
    """Record an engagement, ending whichever open one it replaces."""
    actor = (created_by or "").strip()
    if not actor:
        raise ValueError("Um contrato tem de registar quem o criou.")
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ValueError("Central desconhecida.")
    if service_kind not in SERVICE_KINDS:
        raise ValueError("Tipo de contrato desconhecido.")
    if source_kind not in CONTRACT_SOURCE_KINDS:
        raise ValueError("Origem de contrato desconhecida.")
    if renewal_status is not None and renewal_status not in RENEWAL_STATUSES:
        raise ValueError("Estado de renovação desconhecido.")
    if valid_to is not None and valid_from is not None and valid_to <= valid_from:
        raise ValueError("A data de fim tem de ser posterior à de início.")
    if annual_value_eur is not None and annual_value_eur < 0:
        raise ValueError("O valor anual não pode ser negativo.")

    # Scoped to this kind: an installation can hold O&M and ESCO at once, and
    # recording one must never close the other.
    _close_open_contracts(session, asset_id=asset_id, service_kind=service_kind, on=valid_from)
    session.flush()

    now = utc_now()
    contract = AssetServiceContract(
        asset_id=asset_id,
        installation_id=asset.installation_id,
        service_kind=service_kind,
        valid_from=valid_from,
        valid_to=valid_to,
        annual_value_eur=annual_value_eur,
        renewal_status=renewal_status,
        last_contact_on=last_contact_on,
        notes=(notes or "").strip() or None,
        source_kind=source_kind,
        provenance_json=provenance or {},
        review_status=review_status,
        review_note=review_note,
        created_by=actor[:120],
        created_at=now,
        updated_at=now,
    )
    session.add(contract)
    session.flush()
    return contract


def _close_open_contracts(session: Session, *, asset_id: int, service_kind: str, on: date | None) -> None:
    """End an open window of one engagement kind the day a new one starts.

    Mirrors `reporting.commercial.close_open_row`, including its refusal to
    guess: a row that already starts on or after the new one is a genuine
    conflict between two statements about the same dates, and resolving it
    silently is how V1 lost the terms that were true last year.

    Scoped to `service_kind`: an installation can hold O&M and ESCO
    simultaneously, so recording a new ESCO engagement must never touch an
    open O&M one for the same asset, and vice versa.
    """
    existing = contracts_for(session, asset_id=asset_id, service_kind=service_kind)
    if on is None:
        # An engagement with an unknown start is fine as the only one of its
        # kind -- two V1 installations are exactly that. What it cannot do is
        # *supersede* another, because it cannot be positioned relative to it.
        if existing:
            raise ValueError(
                "Um contrato sem data de início não pode substituir outro; "
                "feche o período anterior explicitamente."
            )
        return
    for contract in existing:
        if contract.valid_from is not None and contract.valid_from >= on:
            raise ValueError(
                f"Já existe um período que começa em {contract.valid_from.isoformat()}; "
                "substitua-o ou apague-o explicitamente em vez de o sobrepor."
            )
        if contract.valid_to is None or contract.valid_to > on:
            contract.valid_to = on
            contract.updated_at = utc_now()


def close_service_contract(
    session: Session, *, contract_id: int, valid_to: date, actor: str
) -> AssetServiceContract:
    """End an engagement on a date, without opening a replacement."""
    contract = session.get(AssetServiceContract, contract_id)
    if contract is None:
        raise ValueError("Contrato desconhecido.")
    if contract.valid_from is not None and valid_to <= contract.valid_from:
        raise ValueError("A data de fim tem de ser posterior à de início.")
    contract.valid_to = valid_to
    contract.updated_at = utc_now()
    session.flush()
    return contract


def update_renewal(
    session: Session,
    *,
    contract_id: int,
    renewal_status: str | None,
    last_contact_on: date | None,
    notes: str | None,
) -> AssetServiceContract:
    """Record renewal follow-up against an engagement, without changing terms."""
    contract = session.get(AssetServiceContract, contract_id)
    if contract is None:
        raise ValueError("Contrato desconhecido.")
    if renewal_status is not None and renewal_status not in RENEWAL_STATUSES:
        raise ValueError("Estado de renovação desconhecido.")
    contract.renewal_status = renewal_status
    contract.last_contact_on = last_contact_on
    contract.notes = (notes or "").strip() or None
    contract.updated_at = utc_now()
    session.flush()
    return contract


def scoped_asset_ids(
    session: Session, *, asset_scope: str, on: date | None = None
) -> set[int] | None:
    """Which installations an asset-scoped feature applies to.

    `None` means "every installation" and is deliberately not an empty set: a
    policy that matches nothing and a policy that matches everything must not
    be the same value, because one of them looks broken and the other is the
    default.
    """
    if asset_scope == "all":
        return None
    if asset_scope == "om":
        return assets_in_om_scope(session, on=on)
    if asset_scope == "om_active":
        return assets_with_active_om(session, on=on)
    raise ValueError("Âmbito de centrais desconhecido.")


def iter_expiring(
    session: Session, *, on: date | None = None, buckets: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Engagements worth acting on, soonest first, for the renewals screen."""
    moment = on or today()
    wanted = set(buckets) if buckets is not None else set(EXPIRY_BUCKETS)
    rows = []
    for asset_id, state in om_status_map(session, on=moment).items():
        if state["status"] == "none" or state["bucket"] not in wanted:
            continue
        rows.append({"asset_id": asset_id, **state})
    return sorted(rows, key=lambda row: (row["valid_to"] or date.max, row["asset_id"]))


def backfill_installation_ids(session: Session) -> dict[str, int]:
    """Fill `installation_id` on every contract row that lacks one.

    Same shape as `installations.service.backfill_installations_from_assets`:
    idempotent, run separately, no row's dates or terms touched -- only the
    new link. A contract whose asset has no Installation yet is left alone
    and reported rather than skipped silently; run the Installation backfill
    first.
    """
    considered = updated = no_installation = 0
    rows = session.scalars(
        select(AssetServiceContract).where(AssetServiceContract.installation_id.is_(None)).order_by(
            AssetServiceContract.id
        )
    ).all()
    now = utc_now()
    for contract in rows:
        considered += 1
        asset = session.get(Asset, contract.asset_id)
        if asset is None or asset.installation_id is None:
            no_installation += 1
            continue
        contract.installation_id = asset.installation_id
        contract.updated_at = now
        updated += 1
    return {"considered": considered, "updated": updated, "no_installation": no_installation}
