"""Behaviour of the shared state vocabulary, not the presence of its strings.

Each test here names a wrong answer the interface used to give and asserts it
cannot come back, rather than asserting that some Portuguese word appears
somewhere on a page.
"""
from __future__ import annotations

from nemsei.providers.models import MAPPING_STATUSES
from nemsei.web.labels import (
    UNLABELLED_MAPPING_STATUSES,
    mapping_state,
    mapping_summary,
    review_state,
)


def test_every_mapping_status_the_model_allows_has_a_label() -> None:
    """A new status must not reach the interface as a raw code.

    This is the guard that made the original defect possible: the model grew
    `invalid` and `superseded`, and the three renderers each invented their
    own answer for them.
    """
    assert UNLABELLED_MAPPING_STATUSES == ()


def test_no_mapping_label_is_the_raw_code() -> None:
    for status in MAPPING_STATUSES:
        label = mapping_state(status)["label"]
        assert label != status
        assert "_" not in label


def test_mapping_labels_are_portuguese_not_capitalised_english() -> None:
    """`superseded`.replace("_"," ").capitalize() produced "Superseded"."""
    assert mapping_state("superseded")["label"] == "Substituído"
    assert mapping_state("invalid")["label"] == "Inválido"
    assert mapping_state("pending_review")["label"] == "Pendente"


def test_an_invalid_mapping_is_not_rendered_as_inert() -> None:
    """`invalid` used to share `superseded`'s neutral grey.

    They are opposite claims: one is history and correct, the other is a plant
    receiving nothing.
    """
    assert mapping_state("invalid")["tone"] == "danger"
    assert mapping_state("superseded")["tone"] == "muted"
    assert mapping_state("invalid")["tone"] != mapping_state("superseded")["tone"]


def test_an_installation_whose_only_mapping_is_invalid_is_never_green() -> None:
    """The defect this module was written for.

    The old verdict asked "are there any mappings at all", so a single
    `invalid` mapping reported "Mapeado" in success green.
    """
    summary = mapping_summary(["invalid"])
    assert summary["tone"] == "danger"
    assert summary["label"] != "Mapeado"


def test_one_broken_mapping_outranks_three_working_ones() -> None:
    """Worst-first. A majority verdict would bury the actionable one."""
    assert mapping_summary(["active", "active", "active", "invalid"])["tone"] == "danger"
    assert mapping_summary(["active", "pending_review"])["tone"] == "warning"
    assert mapping_summary(["active", "active"])["tone"] == "success"


def test_only_superseded_mappings_is_not_mapped_and_not_an_error() -> None:
    """Every mapping is history: nothing feeds this plant, but nothing broke."""
    summary = mapping_summary(["superseded", "superseded"])
    assert summary["label"] != "Mapeado"
    assert summary["tone"] == "warning"


def test_no_mappings_at_all_is_muted_not_a_fault() -> None:
    assert mapping_summary([]) == {"label": "Sem mapping", "tone": "muted"}


def test_an_unknown_status_stays_visible_instead_of_being_swallowed() -> None:
    state = mapping_state("something_new")
    assert state["label"] == "something_new"
    assert state["tone"] == "muted"


def test_review_state_never_says_ok() -> None:
    """"OK" in green, next to an installation whose real state was "Sem leitura".

    The review flag answers "has a person checked this import row". Phrasing it
    as OK let it be read as a verdict on the plant.
    """
    for status in ("clear", "needs_review", None, ""):
        assert review_state(status)["label"] != "OK"


def test_a_clean_review_flag_is_not_rendered_as_a_success_signal() -> None:
    """It was `success` green -- the same tone as a working plant."""
    assert review_state("clear")["tone"] == "muted"
    assert review_state("needs_review")["tone"] == "warning"
