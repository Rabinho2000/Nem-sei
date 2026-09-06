"""Record whether an availability figure is contractual or operational.

Adds `source_kind` to `device_availability_daily`/`asset_availability_daily`,
plus the CHECK constraints that make the source->kind mapping structural
(`reporting/rules/availability_source.py`).

Written to run over a **populated** database, not just an empty one. Every
row that exists when this runs was written by
`diagnostics/availability_service.py`, whose only source is
`fusionsolar_sampled` -- a port of V1's *sampled* engine and therefore
operational, never a WAT. The backfill below states exactly that, and is
keyed on `source` rather than blanket-defaulting, so a row from some other
source (there is none today, but the column is not the place to assume that)
would fail the pair constraint loudly instead of being silently relabelled.

Downgrade drops only what this migration added. No availability row is
deleted in either direction.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_availability_source_kind"
down_revision = "0040_reporting_availability"
branch_labels = None
depends_on = None

_TABLES = ("device_availability_daily", "asset_availability_daily")

# Kept as a literal, not imported from the application: a migration has to
# keep meaning the same thing when the code above it moves on.
_SOURCES = ("fusionsolar_sampled", "provider_wat", "provider_device_availability", "manual")
_KINDS = ("contractual", "operational")
_PAIRS = (
    ("fusionsolar_sampled", "operational"),
    ("manual", "contractual"),
    ("provider_device_availability", "contractual"),
    ("provider_wat", "contractual"),
)
_PAIR_SQL = " OR ".join(f"(source = '{source}' AND source_kind = '{kind}')" for source, kind in _PAIRS)


def _values(options: tuple[str, ...]) -> str:
    """Render a tuple as a SQL `IN` list.

    Same helper as 0039, and for the same reason: `repr` of a one-element
    Python tuple carries a trailing comma, which is a syntax error inside
    `IN (...)`. The downgrade below rebuilds exactly such a one-element
    list.
    """
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    for table in _TABLES:
        # Added nullable, backfilled, then made NOT NULL -- the three-step
        # shape this schema needs on a populated table, since there is no
        # single server_default that is correct for every future source.
        op.add_column(table, sa.Column("source_kind", sa.String(length=16), nullable=True))
        op.execute(
            sa.text(f"UPDATE {table} SET source_kind = 'operational' WHERE source = 'fusionsolar_sampled'")  # noqa: S608
        )
        op.execute(
            sa.text(  # noqa: S608
                f"UPDATE {table} SET source_kind = 'contractual' "
                f"WHERE source IN ('provider_wat', 'provider_device_availability', 'manual')"
            )
        )
        op.alter_column(table, "source_kind", nullable=False)

        # Widen the existing source CHECK to the full vocabulary before the
        # pair constraint references it. The old constraint allowed only
        # 'fusionsolar_sampled', so a contractual row could not be inserted
        # at all until this runs.
        op.drop_constraint(f"ck_{table[: -len('_availability_daily')]}_availability_daily_source", table, type_="check")
        op.create_check_constraint(
            f"ck_{table[: -len('_availability_daily')]}_availability_daily_source",
            table,
            sa.text(f"source IN ({_values(_SOURCES)})"),
        )
        op.create_check_constraint(
            f"ck_{table[: -len('_availability_daily')]}_availability_daily_source_kind",
            table,
            sa.text(f"source_kind IN ({_values(_KINDS)})"),
        )
        op.create_check_constraint(
            f"ck_{table[: -len('_availability_daily')]}_availability_daily_source_kind_pair",
            table,
            sa.text(_PAIR_SQL),
        )

    # The uniqueness key has to carry the source, or the two kinds can never
    # coexist for the same day -- and "prefer contractual over operational"
    # is unreachable if only one of them can be stored at a time. Widening it
    # cannot fail on existing data: before this migration there was exactly
    # one source, so no (key + source) collision can exist.
    op.drop_constraint("uq_device_availability_daily_day", "device_availability_daily", type_="unique")
    op.create_unique_constraint(
        "uq_device_availability_daily_day", "device_availability_daily", ["device_id", "availability_date", "source"]
    )
    op.drop_constraint("uq_asset_availability_daily_day", "asset_availability_daily", type_="unique")
    op.create_unique_constraint(
        "uq_asset_availability_daily_day", "asset_availability_daily", ["asset_id", "availability_date", "source"]
    )


def downgrade() -> None:
    # Narrow the uniqueness key back first. This *can* fail, deliberately: if
    # more than one source has been materialized for the same day, collapsing
    # the key would have to discard one of them, and silently choosing which
    # commercial figure to destroy is not something a downgrade may do.
    op.drop_constraint("uq_asset_availability_daily_day", "asset_availability_daily", type_="unique")
    op.create_unique_constraint("uq_asset_availability_daily_day", "asset_availability_daily", ["asset_id", "availability_date"])
    op.drop_constraint("uq_device_availability_daily_day", "device_availability_daily", type_="unique")
    op.create_unique_constraint("uq_device_availability_daily_day", "device_availability_daily", ["device_id", "availability_date"])

    for table in _TABLES:
        prefix = f"ck_{table[: -len('_availability_daily')]}_availability_daily"
        op.drop_constraint(f"{prefix}_source_kind_pair", table, type_="check")
        op.drop_constraint(f"{prefix}_source_kind", table, type_="check")
        op.drop_constraint(f"{prefix}_source", table, type_="check")
        # Restore the narrower pre-0041 vocabulary. Safe because no writer
        # before this migration could have produced any other source; a row
        # with a contractual source would block the downgrade, which is the
        # honest outcome -- dropping the column would discard what it means.
        op.create_check_constraint(f"{prefix}_source", table, sa.text(f"source IN ({_values(('fusionsolar_sampled',))})"))
        op.drop_column(table, "source_kind")
