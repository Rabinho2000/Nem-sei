"""Per-connection production scheduling, persisted on the connection itself.

Until now exactly one FusionSolar connection could have a scheduled
production sync, because the scheduler read a single environment variable
(`NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_CONNECTION_ID`). That was the right
restraint for the rollout -- a shared, rate-limited account is not a place
to loop over whatever happens to be configured -- but it does not scale to
the fleet, and it made "which connections are synced" a deploy-time fact
that nothing in the database could answer.

Three columns, all nullable or defaulted, so this runs over a populated
table without rewriting a row:

* `production_sync_enabled` -- explicit, per-connection eligibility. Default
  `false`: adding a connection never starts calling a provider, and the
  fleet still cannot be swept blindly, because a connection has to be turned
  on one at a time.
* `production_sync_interval_hours` -- an optional per-connection cadence, so
  a second account with a different tolerance does not force the first one
  to share its interval. `NULL` means "use the global default".
* `initial_production_from_date` -- the bootstrap date. A connection with no
  production cursor cannot run an incremental sync (the service refuses:
  "The first production sync requires an explicit start date"), and nothing
  should guess how far back a plant's history goes. With this set, the
  first job is a bounded backfill from that date; without it the connection
  reports as "produção não inicializada" and makes no provider call at all.

Downgrade drops the three columns. It cannot lose anything derived: the
cursors, the jobs and the facts all live elsewhere.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_production_scheduling"
down_revision = "0043_device_history_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_connections",
        sa.Column("production_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("provider_connections", sa.Column("production_sync_interval_hours", sa.Integer(), nullable=True))
    op.add_column("provider_connections", sa.Column("initial_production_from_date", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_provider_connections_production_interval",
        "provider_connections",
        "production_sync_interval_hours IS NULL OR production_sync_interval_hours > 0",
    )
    # The scheduler asks "which connections are due" on every tick; without
    # this it is a sequential scan of the whole table each time.
    op.create_index(
        "ix_provider_connections_production_sync",
        "provider_connections",
        ["production_sync_enabled", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_connections_production_sync", table_name="provider_connections")
    op.drop_constraint("ck_provider_connections_production_interval", "provider_connections", type_="check")
    op.drop_column("provider_connections", "initial_production_from_date")
    op.drop_column("provider_connections", "production_sync_interval_hours")
    op.drop_column("provider_connections", "production_sync_enabled")
