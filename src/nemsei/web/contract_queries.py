"""What the O&M contract screens render.

Labels and tones live here rather than in the templates so that "expirado"
means the same thing, and looks the same, on the list, on the installation and
on the renewals page.

Colour follows the rule the rest of this interface already follows: the normal
state is silent. An installation under a live contract is `muted`, not green --
green would make three quarters of a healthy table shout. Only a lapse, or a
renewal window closing, earns attention.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, Organization
from nemsei.contracts.models import RENEWAL_STATUSES
from nemsei.contracts.service import (
    EXPIRY_BUCKETS,
    contracts_for,
    iter_expiring,
    om_status_map,
    overview,
    today,
)

OM_STATUS_LABELS = {
    "active": "O&M ativo",
    "expired": "Contrato expirado",
    "undated": "O&M sem datas",
    "none": "Sem O&M",
}
OM_STATUS_TONES = {
    "active": "muted",
    "expired": "warning",
    "undated": "warning",
    "none": "muted",
}
BUCKET_LABELS = {
    "expired": "Expirado",
    "within_90_days": "Expira em 90 dias",
    "within_a_year": "Expira este ano",
    "beyond_a_year": "Mais de um ano",
    "undated": "Sem data de fim",
}
BUCKET_TONES = {
    "expired": "danger",
    "within_90_days": "warning",
    "within_a_year": "muted",
    "beyond_a_year": "muted",
    "undated": "warning",
}
RENEWAL_LABELS = {
    "not_started": "Por iniciar",
    "in_contact": "Em contacto",
    "renewed": "Renovado",
    "lost": "Perdido",
}
# Which buckets the renewals screen shows by default: the work, not the archive.
ACTIONABLE_BUCKETS = ("expired", "within_90_days", "undated")
# The filter vocabulary the list offers, mapped to the derived statuses it keeps.
LIST_FILTERS = {
    "ativo": ("active", "undated"),
    "expirado": ("expired",),
    "sem": ("none",),
}


def decorate(state: dict[str, Any]) -> dict[str, Any]:
    """Attach the labels and tone one derived O&M state renders with."""
    status = state["status"]
    bucket = state.get("bucket")
    # `valid_to` is exclusive, so the date a human calls "the end" is the day
    # before it. Computed once here rather than in each template, which is how
    # an off-by-one gets into a screen.
    last_covered_day = state["valid_to"] - timedelta(days=1) if state["valid_to"] else None
    return {
        **state,
        "last_covered_day": last_covered_day,
        "label": OM_STATUS_LABELS[status],
        "tone": OM_STATUS_TONES[status],
        "bucket_label": BUCKET_LABELS.get(bucket) if bucket else None,
        "bucket_tone": BUCKET_TONES.get(bucket) if bucket else None,
        "renewal_label": RENEWAL_LABELS.get(state["contract"].renewal_status) if state["contract"] else None,
    }


def om_states_for(session: Session, *, asset_ids: list[int], on: date | None = None) -> dict[int, dict[str, Any]]:
    return {
        asset_id: decorate(state)
        for asset_id, state in om_status_map(session, asset_ids=asset_ids, on=on).items()
    }


def om_filter_ids(session: Session, *, om: str, on: date | None = None) -> set[int] | None:
    """Asset ids matching a list filter, or None when the filter is off.

    Filtering happens before pagination, in SQL, because a filter applied to
    the current page would report a page count for a different question than
    the one the operator asked.
    """
    wanted = LIST_FILTERS.get(om)
    if wanted is None:
        return None
    keep = set(wanted)
    if "none" in keep:
        # Every installation without an engagement -- the complement of the
        # contract table, which `om_status_map` only reports for assets it is
        # asked about.
        with_contract = set(om_status_map(session, on=on))
        every = set(session.scalars(select(Asset.id)))
        return every - with_contract
    return {
        asset_id
        for asset_id, state in om_status_map(session, on=on).items()
        if state["status"] in keep
    }


def asset_contract_panel(session: Session, *, asset_id: int, on: date | None = None) -> dict[str, Any]:
    """The contract history and current state for one installation."""
    moment = on or today()
    state = decorate(om_status_map(session, asset_ids=[asset_id], on=moment)[asset_id])
    history = [
        {
            "id": contract.id,
            "valid_from": contract.valid_from,
            "valid_to": contract.valid_to,
            "last_covered_day": contract.valid_to - timedelta(days=1) if contract.valid_to else None,
            "annual_value_eur": contract.annual_value_eur,
            "renewal_status": contract.renewal_status,
            "renewal_label": RENEWAL_LABELS.get(contract.renewal_status),
            "last_contact_on": contract.last_contact_on,
            "notes": contract.notes,
            "source_kind": contract.source_kind,
            "review_status": contract.review_status,
            "review_note": contract.review_note,
            "is_current": contract.covers(moment),
        }
        for contract in contracts_for(session, asset_id=asset_id)
    ]
    return {
        "om": state,
        "om_history": history,
        "om_renewal_options": [(value, RENEWAL_LABELS[value]) for value in RENEWAL_STATUSES],
    }


def contracts_page_data(session: Session, *, bucket: str = "", on: date | None = None) -> dict[str, Any]:
    """The renewals screen: V1's `renewals`, over evidence V2 can derive."""
    moment = on or today()
    wanted = (bucket,) if bucket in EXPIRY_BUCKETS else ACTIONABLE_BUCKETS
    rows = iter_expiring(session, on=moment, buckets=wanted)
    asset_ids = [row["asset_id"] for row in rows]
    names = {
        asset_id: (name, organization)
        for asset_id, name, organization in session.execute(
            select(Asset.id, Asset.canonical_name, Organization.display_name)
            .outerjoin(Organization, Organization.id == Asset.owner_id)
            .where(Asset.id.in_(asset_ids))
        )
    } if asset_ids else {}
    items = []
    for row in rows:
        name, organization = names.get(row["asset_id"], ("(desconhecida)", None))
        items.append({**decorate(row), "canonical_name": name, "organization_name": organization})
    summary = overview(session, on=moment)
    return {
        "contracts": items,
        "summary": summary,
        "bucket_counts": [
            (value, BUCKET_LABELS[value], BUCKET_TONES[value], summary["buckets"].get(value, 0))
            for value in EXPIRY_BUCKETS
        ],
        "selected_bucket": bucket if bucket in EXPIRY_BUCKETS else "",
        "renewal_options": [(value, RENEWAL_LABELS[value]) for value in RENEWAL_STATUSES],
        "as_of": moment,
    }
