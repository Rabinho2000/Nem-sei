"""One place that turns an internal state code into what a person reads.

Mapping status was rendered in three different places, three different ways,
and all three disagreed. `queries._mapping_summaries` called an asset
"Mapeado", in green, whenever it had any open mapping at all -- including one
the platform itself had marked `invalid`, which is the opposite of the truth.
The asset detail ran `mapping_status.replace("_", " ").capitalize()`, which
turns `superseded` into the English word "Superseded" on a Portuguese page.
`mappings.html` printed the raw code, so an operator read "pending_review".

A state code is not a label, and deriving one from the other by string
manipulation is how English leaks into a Portuguese interface. The vocabulary
below is the single source; the tone travels with it, because "what colour is
this" and "what does this say" must never be answered by two separate
expressions that can drift apart.
"""
from __future__ import annotations


from nemsei.providers.models import MAPPING_STATUSES


# A tone is a claim about whether the operator needs to do something:
#   success -- working, nothing to do
#   warning -- someone must decide
#   danger  -- broken, and it will not fix itself
#   muted   -- true but inert, no action available
#
# `invalid` is `danger`, not `muted`. A mapping the platform rejected is a
# plant that silently receives no data; rendering it the same neutral grey as
# `superseded` (which is merely history, and correct) hides a live fault.
MAPPING_STATE_LABELS: dict[str, tuple[str, str]] = {
    "active": ("Ativo", "success"),
    "pending_review": ("Pendente", "warning"),
    "invalid": ("Inválido", "danger"),
    "superseded": ("Substituído", "muted"),
}

# Vocabulary drift guard: a status added to the model without a label here
# would otherwise reach the interface as a raw code, which is the exact defect
# this module exists to remove. `test_labels.py` asserts this is empty.
UNLABELLED_MAPPING_STATUSES = tuple(sorted(set(MAPPING_STATUSES) - set(MAPPING_STATE_LABELS)))


def mapping_state(status: str | None) -> dict[str, str]:
    """One mapping status as label and tone. Unknown codes stay visible."""
    label, tone = MAPPING_STATE_LABELS.get(status or "", (status or "—", "muted"))
    return {"status": status or "", "label": label, "tone": tone}


def mapping_summary(statuses: list[str] | tuple[str, ...]) -> dict[str, str]:
    """What one installation's whole set of open mappings amounts to.

    Worst-first, deliberately. An installation with one broken mapping and
    three working ones has a problem, and reporting the majority verdict would
    bury it. The order is the order of how much attention each state needs.
    """
    if not statuses:
        return {"label": "Sem mapping", "tone": "muted"}
    for status, label in (("invalid", "Mapping inválido"), ("pending_review", "Pendente")):
        if status in statuses:
            return {"label": label, "tone": MAPPING_STATE_LABELS[status][1]}
    if "active" in statuses:
        return {"label": "Mapeado", "tone": "success"}
    # Only superseded left: every mapping this installation had is history, so
    # nothing is feeding it. That is not "Mapeado" and it is not an error --
    # it is a plant nobody has reconnected.
    return {"label": "Sem mapping ativo", "tone": "warning"}


# The administrative review flag left by the V1 importer. It answers "has a
# person checked this row", never "is this plant working" -- the operational
# question is `installation_state`. They used to share the word "Estado" and
# the value "OK", so a plant with no reading at all could show a green OK.
REVIEW_STATE_LABELS: dict[str, tuple[str, str]] = {
    "clear": ("Revista", "muted"),
    "needs_review": ("Precisa de revisão", "warning"),
}


def review_state(status: str | None) -> dict[str, str]:
    """The import review flag, phrased so it cannot be read as plant health."""
    label, tone = REVIEW_STATE_LABELS.get(status or "", (status or "—", "muted"))
    return {"status": status or "", "label": label, "tone": tone}

