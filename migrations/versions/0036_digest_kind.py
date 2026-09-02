"""digest_runs.kind: one table, three kinds of periodic summary.

Telegram O&M redesign, Fatia 4 (see the session's plan and
`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` §30). Req 13's grouped
recovery digest and reqs 10/11's morning briefing are both "a periodic
summary, delivered at most once to at most one channel", exactly what
`DigestRun` (D6) already is -- so this is one column and one constraint
change, not a second table and not a second delivery mechanism.
`notifications/digests.py::deliver_digest` is reused unchanged for every
kind; only the payload/render builders differ.

`kind` joins the identity: `(kind, window_start, window_end)`, not just
`(window_start, window_end)` -- the three kinds run on independent
schedules (the existing diagnostics digest's own interval, a 2h recovery
grouping, a daily 09:00 briefing) and must window-chain independently, each
against its own most recent row of its own kind, never another kind's.

Additive only. Every existing `digest_runs` row (D6's own diagnostics
digest, the only kind before this migration) backfills to `kind='diagnostics'`
via the column's own server default -- nothing about what those rows already
mean changes.

Revision ID: 0036_digest_kind
Revises: 0035_notification_episodes
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_digest_kind"
down_revision = "0035_notification_episodes"
branch_labels = None
depends_on = None

DIGEST_KINDS = ("diagnostics", "recoveries", "morning_briefing")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.add_column("digest_runs", sa.Column("kind", sa.String(length=32), nullable=False, server_default="diagnostics"))
    op.create_check_constraint("ck_digest_runs_kind", "digest_runs", f"kind IN ({_values(DIGEST_KINDS)})")
    op.drop_constraint("uq_digest_runs_window", "digest_runs", type_="unique")
    op.create_unique_constraint("uq_digest_runs_window", "digest_runs", ["kind", "window_start", "window_end"])
    op.create_index("ix_digest_runs_kind_window_end", "digest_runs", ["kind", "window_end"])


def downgrade() -> None:
    non_diagnostics = op.get_bind().execute(
        sa.text("SELECT count(*) FROM digest_runs WHERE kind <> 'diagnostics'")
    ).scalar_one()
    if non_diagnostics:
        raise RuntimeError(
            f"Refusing to downgrade: {non_diagnostics} recovery/briefing digests have no home in the old schema."
        )
    op.drop_index("ix_digest_runs_kind_window_end", table_name="digest_runs")
    op.drop_constraint("uq_digest_runs_window", "digest_runs", type_="unique")
    op.create_unique_constraint("uq_digest_runs_window", "digest_runs", ["window_start", "window_end"])
    op.drop_constraint("ck_digest_runs_kind", "digest_runs", type_="check")
    op.drop_column("digest_runs", "kind")
