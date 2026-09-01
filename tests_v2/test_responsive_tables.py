"""The dense tables must survive a phone without horizontal scrolling.

These assert structure, not appearance: that every data cell carries the
label its card layout reads, and that the header row count and the empty-row
colspan agree. A screenshot test would be better and is not available here;
what is testable is that the markup cannot silently lose a column's name.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "nemsei" / "web" / "templates"
STYLES = Path(__file__).resolve().parents[1] / "src" / "nemsei" / "web" / "static" / "styles.css"

# Tables that must read as cards on a phone. Adding one here without adding
# `stack-mobile` and the data-labels to its template fails these tests.
STACKED_TABLES = ["assets/list.html"]


def read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", STACKED_TABLES)
def test_the_table_opts_into_the_card_layout(name: str) -> None:
    assert "stack-mobile" in read(name)


@pytest.mark.parametrize("name", STACKED_TABLES)
def test_every_data_cell_carries_its_column_name(name: str) -> None:
    """A cell without `data-label` renders as a value with nothing naming it.

    `pick-column` is the deliberate exception: a checkbox is not a
    label/value pair, and the CSS renders it without one.
    """
    body = read(name).split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    unlabelled = [
        cell
        for cell in re.findall(r"<td\b[^>]*>", body)
        if "data-label=" not in cell and "pick-column" not in cell and "table-empty" not in cell
    ]
    assert unlabelled == []


@pytest.mark.parametrize("name", STACKED_TABLES)
def test_the_empty_row_spans_the_whole_table(name: str) -> None:
    """A colspan short of the header count leaves a ragged empty state.

    The assets list said 11 against a 12-column header.
    """
    source = read(name)
    header = source.split("<thead>", 1)[1].split("</thead>", 1)[0]
    columns = len(re.findall(r"<th\b", header))
    for span in re.findall(r'class="table-empty" colspan="(\d+)"', source):
        assert int(span) == columns


def test_the_card_layout_reads_its_labels_from_the_markup() -> None:
    """`content: attr(data-label)` -- never a second copy of the column names.

    A hard-coded list of headings inside the CSS would drift away from the
    <thead> the moment a column is renamed.
    """
    assert "content: attr(data-label)" in STYLES.read_text(encoding="utf-8")


def test_the_header_stays_available_to_screen_readers() -> None:
    """Hidden visually, not removed. `display: none` would drop it from the
    accessibility tree along with the column semantics."""
    css = STYLES.read_text(encoding="utf-8")
    rule = css.split(".stack-mobile thead {", 1)[1].split("}", 1)[0]
    assert "display: none" not in rule
    assert "clip-path" in rule
