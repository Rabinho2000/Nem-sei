"""Give an incident a human lifecycle, kept separate from the detector's own.

`diagnostic_incidents.status` belongs to the machine: `incidents.py` sets it to
`resolved` when a rule stops being true, guarded by a CHECK tying it to
`resolved_at` and by a partial unique index that allows only one `open` row per
identity. Widening that vocabulary with human states would break both -- an
operator marking an incident "done" would free the identity and let the very
next evaluation open a duplicate for a condition that never went away.

So the human dimension is its own column. The two are orthogonal on purpose:
a still-detected incident can legitimately be `done` ("seen, known, not
acting"), and a detector-resolved one can still be `investigating`. That is
real O&M, not an edge case.

`incident_notes` is append-only, like every other record of a decision in this
schema. `assigned_to` is free text until users exist (§16); recording a name
badly is better than not recording who owns 644 open incidents at all.

Revision ID: 0023_incident_handling
Revises: 0022_source_file_content
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_incident_handling"
down_revision = "0022_source_file_content"
branch_labels = None
depends_on = None

HANDLING_STATES = ("new", "acknowledged", "investigating", "visit_scheduled", "done")


def upgrade() -> None:
    op.add_column(
        "diagnostic_incidents",
        sa.Column("handling_state", sa.String(24), nullable=False, server_default="new"),
    )
    op.add_column("diagnostic_incidents", sa.Column("assigned_to", sa.String(120), nullable=True))
    op.add_column("diagnostic_incidents", sa.Column("handling_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_diagnostic_incidents_handling_state",
        "diagnostic_incidents",
        "handling_state IN {!r}".format(HANDLING_STATES),
    )
    op.create_index(
        "ix_diagnostic_incidents_handling",
        "diagnostic_incidents",
        ["handling_state", "status"],
    )
    op.create_table(
        "incident_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("diagnostic_incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author", sa.String(120), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        # What the incident moved to when this note was written, or NULL for a
        # note that only comments. Reading the column back gives the whole
        # handling history without a second table.
        sa.Column("handling_state_after", sa.String(24), nullable=True),
        sa.Column("assigned_to_after", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("body IS NOT NULL OR handling_state_after IS NOT NULL", name="ck_incident_notes_not_empty"),
    )
    op.create_index("ix_incident_notes_incident", "incident_notes", ["incident_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_incident_notes_incident", table_name="incident_notes")
    op.drop_table("incident_notes")
    op.drop_index("ix_diagnostic_incidents_handling", table_name="diagnostic_incidents")
    op.drop_constraint("ck_diagnostic_incidents_handling_state", "diagnostic_incidents", type_="check")
    op.drop_column("diagnostic_incidents", "handling_updated_at")
    op.drop_column("diagnostic_incidents", "assigned_to")
    op.drop_column("diagnostic_incidents", "handling_state")
