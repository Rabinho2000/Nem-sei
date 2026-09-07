"""Admit closed-day device history as evidence, and its derived contractual source.

Two small vocabulary widenings, no new table:

1. `device_status_facts.source_kind` gains `history_read` -- rows pulled from
   `/thirdData/getDevHistoryKpi` for a closed day. They are device status
   readings like the others (same fields, same normalizer, same revision
   chain), so they belong in the same table; what distinguishes them is that
   they come from a dense 5-minute historical series rather than a sparse
   realtime poll, which is what makes them contract-grade.
2. `device_availability_daily`/`asset_availability_daily`'s `source` gains
   `fusionsolar_device_history`, paired to `contractual`.

Runs over a populated database: both changes are pure widenings of CHECK
constraints, so no existing row can violate the new form and nothing is
rewritten. Downgrade narrows them back, and will fail loudly -- by design --
if rows using the new vocabulary exist, rather than deleting them.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_device_history_facts"
down_revision = "0042_dataset_availability_source"
branch_labels = None
depends_on = None

_OLD_FACT_KINDS = ("v1_import", "live_read")
_NEW_FACT_KINDS = ("v1_import", "live_read", "history_read")
_OLD_SOURCES = ("fusionsolar_sampled", "provider_wat", "provider_device_availability", "manual")
_NEW_SOURCES = (*_OLD_SOURCES, "fusionsolar_device_history")
_OLD_PAIRS = (
    ("fusionsolar_sampled", "operational"),
    ("manual", "contractual"),
    ("provider_device_availability", "contractual"),
    ("provider_wat", "contractual"),
)
_NEW_PAIRS = tuple(sorted((*_OLD_PAIRS, ("fusionsolar_device_history", "contractual"))))
_AVAILABILITY_TABLES = ("device_availability_daily", "asset_availability_daily")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def _pairs(pairs: tuple[tuple[str, str], ...]) -> str:
    return " OR ".join(f"(source = '{source}' AND source_kind = '{kind}')" for source, kind in pairs)


def _prefix(table: str) -> str:
    return f"ck_{table[: -len('_availability_daily')]}_availability_daily"


def _rewrite(*, fact_kinds: tuple[str, ...], sources: tuple[str, ...], pairs: tuple[tuple[str, str], ...]) -> None:
    op.drop_constraint("ck_device_status_facts_source_kind", "device_status_facts", type_="check")
    op.create_check_constraint(
        "ck_device_status_facts_source_kind", "device_status_facts", sa.text(f"source_kind IN ({_values(fact_kinds)})")
    )
    for table in _AVAILABILITY_TABLES:
        op.drop_constraint(f"{_prefix(table)}_source_kind_pair", table, type_="check")
        op.drop_constraint(f"{_prefix(table)}_source", table, type_="check")
        op.create_check_constraint(f"{_prefix(table)}_source", table, sa.text(f"source IN ({_values(sources)})"))
        op.create_check_constraint(f"{_prefix(table)}_source_kind_pair", table, sa.text(_pairs(pairs)))


def upgrade() -> None:
    _rewrite(fact_kinds=_NEW_FACT_KINDS, sources=_NEW_SOURCES, pairs=_NEW_PAIRS)
    # `reporting_dataset_rows` carries the same source vocabulary (0042).
    op.drop_constraint("ck_reporting_dataset_rows_availability_source", "reporting_dataset_rows", type_="check")
    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_source",
        "reporting_dataset_rows",
        sa.text(f"availability_source IS NULL OR availability_source IN ({_values(_NEW_SOURCES)})"),
    )


def downgrade() -> None:
    _rewrite(fact_kinds=_OLD_FACT_KINDS, sources=_OLD_SOURCES, pairs=_OLD_PAIRS)
    op.drop_constraint("ck_reporting_dataset_rows_availability_source", "reporting_dataset_rows", type_="check")
    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_source",
        "reporting_dataset_rows",
        sa.text(f"availability_source IS NULL OR availability_source IN ({_values(_OLD_SOURCES)})"),
    )
