"""What Solcor sells at an installation, and how urgent its problems therefore are.

Two derived things live here, both read-only and both computed from evidence the
database already holds.

**Commercial family** classifies `assets.contract_type`, which V1 wrote as free
text and V2 deliberately kept that way (`assets/models.py`) because normalising
would lose distinctions its operators made.

**Service priority** is why that classification matters operationally. Under an
ESCO the installation is Solcor's and Solcor sells the energy, so an hour of
downtime is Solcor's lost revenue. Under an EPC the customer owns the plant and
the same hour is the customer's loss, reported and invoiced but not billed to
Solcor. The 68 ESCO installations under a live O&M contract are 81% of the
whole operated portfolio, so this is not a rare corner.

## Why this is not `detect_report_type_value`

`reporting/rules/billing.py` already classifies the same column, and it must
keep answering differently. It decides *which document a customer receives*, so
"ESCO BUYOUT" is an ESCO there: the arrangement was an ESCO and the report has
to read like one. Here the question is *whose money is at risk right now*, and a
bought-out system is no longer Solcor's to lose revenue on. All five of V1's
ESCO BUYOUT installations carry no O&M engagement at all, which is the same
answer arriving from the other direction.

Do not "fix" the divergence by making one call the other. They are two
questions that happen to read one column.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from nemsei.contracts.service import om_status_map

# `esco_buyout` is its own value rather than a flavour of `esco` -- see the
# module docstring. `unknown` covers the 14 installations whose contract type
# V1 left empty; it is not a synonym for EPC, and calling it one would quietly
# assert a commercial arrangement nobody recorded.
COMMERCIAL_FAMILIES = ("esco", "esco_buyout", "epc", "unknown")
SERVICE_PRIORITIES = ("high", "normal", "low")

FAMILY_LABELS = {
    "esco": "ESCO",
    "esco_buyout": "ESCO buyout",
    "epc": "EPC",
    "unknown": "Sem contrato registado",
}
# ESCO is the only family that earns a colour, and only because it changes what
# an operator should do first. The rest stay silent, per the control-room rule.
FAMILY_TONES = {"esco": "accent", "esco_buyout": "muted", "epc": "muted", "unknown": "muted"}

PRIORITY_LABELS = {"high": "Prioritária", "normal": "Normal", "low": "Fora de O&M"}
PRIORITY_TONES = {"high": "warning", "normal": "muted", "low": "muted"}
PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
PRIORITY_REASONS = {
    "high": "ESCO com O&M em vigor: a energia é vendida pela Solcor, a paragem é receita perdida.",
    "normal": "Com O&M em vigor. A paragem é perda do cliente, reportada mas não faturada à Solcor.",
    "low": "Sem contrato O&M em vigor.",
}


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return text.lower().strip()


def commercial_family(contract_type: str | None) -> str:
    """Classify one `contract_type` value. Order matters: buyout before ESCO."""
    value = _normalized(contract_type)
    if not value:
        return "unknown"
    if re.search(r"(^|\W)buy\s*out($|\W)", value) or "buyout" in value:
        return "esco_buyout"
    if re.search(r"(^|\W)esco($|\W)", value):
        return "esco"
    if re.search(r"(^|\W)epc($|\W)", value):
        return "epc"
    return "unknown"


def service_priority(*, family: str, om_status: str) -> str:
    """How urgently a problem at this installation should be worked.

    Deliberately does not consult severity. Severity says how broken something
    is; priority says whose money is running out while it stays broken. Folding
    them into one number would let an ESCO warning outrank a plant that is
    actually down.
    """
    if om_status not in {"active", "undated"}:
        return "low"
    return "high" if family == "esco" else "normal"


def describe(contract_type: str | None, om_status: str) -> dict[str, Any]:
    family = commercial_family(contract_type)
    priority = service_priority(family=family, om_status=om_status)
    return {
        "contract_type": (contract_type or "").strip() or None,
        "family": family,
        "family_label": FAMILY_LABELS[family],
        "family_tone": FAMILY_TONES[family],
        "priority": priority,
        "priority_label": PRIORITY_LABELS[priority],
        "priority_tone": PRIORITY_TONES[priority],
        "priority_reason": PRIORITY_REASONS[priority],
        "priority_rank": PRIORITY_ORDER[priority],
    }


def describe_assets(
    session: Session, *, assets: dict[int, str | None], on: date | None = None
) -> dict[int, dict[str, Any]]:
    """Family and priority for a set of installations, in one pass.

    `assets` maps asset id to its raw `contract_type`, so a caller that already
    loaded the rows does not make this module load them again.
    """
    statuses = om_status_map(session, asset_ids=list(assets), on=on)
    return {
        asset_id: describe(contract_type, statuses[asset_id]["status"])
        for asset_id, contract_type in assets.items()
    }
