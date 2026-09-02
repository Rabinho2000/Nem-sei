"""Operational contacts for an Installation.

Req 8 of the Telegram O&M redesign
(`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`): an alert needs to be able
to say who to call, without ever inventing a number. A missing contact is a
real, common state -- most of the 267 installations have never had one
recorded -- so `installation_contacts` is deliberately allowed to have zero
rows for an installation; the renderer reads that as "não registado", not as
an error.

One installation can hold several contacts (client, facility manager, local
maintenance, security, owner...); `is_primary` marks which one a renderer
should reach for first, but does not make the others invisible -- more than
one contact can exist without one being "the" contact.

New table only. Nothing existing changes.

Revision ID: 0034_installation_contacts
Revises: 0033_work_orders
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_installation_contacts"
down_revision = "0033_work_orders"
branch_labels = None
depends_on = None

CONTACT_TYPES = ("client", "facility_manager", "local_maintenance", "security", "owner", "other")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.create_table(
        "installation_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "installation_id", sa.Integer(), sa.ForeignKey("installations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("role", sa.String(length=120)),
        sa.Column("phone", sa.String(length=64)),
        sa.Column("email", sa.String(length=255)),
        sa.Column("contact_type", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_check_constraint(
        "ck_installation_contacts_type", "installation_contacts", f"contact_type IN ({_values(CONTACT_TYPES)})"
    )
    # A contact with neither a phone nor an email is not reachable at all --
    # not invalid to *record* (someone may only know a name and a role so
    # far), but the check makes "reachable" and "known" distinguishable at
    # the schema level rather than only in application code.
    op.create_check_constraint(
        "ck_installation_contacts_reachable",
        "installation_contacts",
        "phone IS NOT NULL OR email IS NOT NULL",
    )
    op.create_index("ix_installation_contacts_installation", "installation_contacts", ["installation_id", "is_primary"])


def downgrade() -> None:
    op.drop_table("installation_contacts")
