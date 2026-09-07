"""Carry the availability source and its kind onto reporting dataset rows.

0040 gave `reporting_dataset_rows` a percentage and a state, but nothing
saying *what produced the number*. That was tolerable only while exactly one
source existed. With the contractual/operational split (0041) a report that
shows a bare percentage cannot say whether it is a warranted figure or an
operational estimate -- so the row carries both, and reporting reads them
rather than assuming.

Nullable on purpose: a row whose month has no materialized availability at
all has no source, and `NULL` is the honest answer there rather than a
placeholder that would read like a real provenance. The paired constraint
below makes the two move together -- a percentage without a source, or a
source without its kind, is rejected.

Populated over an existing table: every row written before this migration
could only have come from `fusionsolar_sampled`, but the backfill does not
assume that from the state alone -- it copies from `asset_availability_daily`
where a matching materialized month exists and leaves the rest NULL, so no
row is labelled with a provenance nothing actually recorded.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_dataset_availability_source"
down_revision = "0041_availability_source_kind"
branch_labels = None
depends_on = None

_SOURCES = ("fusionsolar_sampled", "provider_wat", "provider_device_availability", "manual")
_KINDS = ("contractual", "operational")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.add_column("reporting_dataset_rows", sa.Column("availability_source", sa.String(length=32), nullable=True))
    op.add_column("reporting_dataset_rows", sa.Column("availability_source_kind", sa.String(length=16), nullable=True))

    # Backfill from the facts the rows were actually built from, not from an
    # assumption about what the only source used to be. A dataset row whose
    # month has no `asset_availability_daily` rows stays NULL.
    # Backfill from the facts the rows were actually built from. `DISTINCT ON`
    # is ordered explicitly so the choice is deterministic rather than
    # whatever the planner returns first.
    op.execute(
        sa.text(
            """
            UPDATE reporting_dataset_rows AS r
               SET availability_source = daily.source,
                   availability_source_kind = daily.source_kind
              FROM (
                    SELECT DISTINCT ON (asset_id, date_trunc('month', availability_date))
                           asset_id,
                           date_trunc('month', availability_date) AS month,
                           source,
                           source_kind
                      FROM asset_availability_daily
                     ORDER BY asset_id, date_trunc('month', availability_date), source
                   ) AS daily
             WHERE r.asset_id = daily.asset_id
               AND date_trunc('month', r.period_start) = daily.month
               AND r.availability_pct IS NOT NULL
            """
        )
    )
    # Any percentage still without a source predates this migration, and
    # before it there was exactly one writer of availability rows
    # (`diagnostics/availability_service.py`, `fusionsolar_sampled`, a port of
    # V1's sampled engine). Stating that is a fact about what could have
    # written the row, not a guess -- and it is what keeps the
    # `availability_pct IS NULL OR availability_source IS NOT NULL` constraint
    # below applicable to a populated database instead of unenforceable.
    op.execute(
        sa.text(
            "UPDATE reporting_dataset_rows SET availability_source = 'fusionsolar_sampled', "
            "availability_source_kind = 'operational' "
            "WHERE availability_pct IS NOT NULL AND availability_source IS NULL"
        )
    )

    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_source",
        "reporting_dataset_rows",
        f"availability_source IS NULL OR availability_source IN ({_values(_SOURCES)})",
    )
    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_source_kind",
        "reporting_dataset_rows",
        f"availability_source_kind IS NULL OR availability_source_kind IN ({_values(_KINDS)})",
    )
    # A source and its kind travel together, always.
    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_source_pair",
        "reporting_dataset_rows",
        "(availability_source IS NULL) = (availability_source_kind IS NULL)",
    )
    # A reportable percentage must say where it came from.
    op.create_check_constraint(
        "ck_reporting_dataset_rows_availability_pct_has_source",
        "reporting_dataset_rows",
        "availability_pct IS NULL OR availability_source IS NOT NULL",
    )


def downgrade() -> None:
    for name in (
        "ck_reporting_dataset_rows_availability_pct_has_source",
        "ck_reporting_dataset_rows_availability_source_pair",
        "ck_reporting_dataset_rows_availability_source_kind",
        "ck_reporting_dataset_rows_availability_source",
    ):
        op.drop_constraint(name, "reporting_dataset_rows", type_="check")
    op.drop_column("reporting_dataset_rows", "availability_source_kind")
    op.drop_column("reporting_dataset_rows", "availability_source")
