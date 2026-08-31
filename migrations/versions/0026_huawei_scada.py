"""Huawei SCADA: the provider code, plus the tables its inbound session writes.

Two independent changes that must land together, because either alone leaves
the database in a state the code cannot use: widening
`ck_provider_connections_provider_code` so a `huawei_scada` connection can
exist at all, and creating the three tables that connection's listener writes
to.

`huawei_scada_power_samples` holds **power**, in kW, at an instant. It is not
a second energy table: `production_facts` still owns energy, and the daily kWh
rows written from these samples are produced by an explicit integration
(`integrations/huawei_scada/rollup.py`) that records its own method and marks
its output estimated. Storing kW in a column a reader might total as kWh is
the single most expensive mistake available here, so the column names carry
their unit.

Downgrade refuses rather than deletes, the same rule every table-creating
migration in this repository follows: a sample that took a month of a
customer's production to collect cannot be recreated by re-running anything.

Revision ID: 0026_huawei_scada
Revises: 0025_inverter_state_40960
"""
from alembic import op
import sqlalchemy as sa


revision = "0026_huawei_scada"
down_revision = "0025_inverter_state_40960"
branch_labels = None
depends_on = None


PROVIDER_CODES_BEFORE = ("fusionsolar", "sigenergy", "sma")
PROVIDER_CODES_AFTER = ("fusionsolar", "sigenergy", "sma", "huawei_scada")
QUALITY_STATES = ("complete", "partial", "missing", "invalid", "unknown")
SESSION_STATES = ("connected", "polling", "degraded", "quarantined", "closed")
CLOSE_REASONS = (
    "peer_closed",
    "idle_timeout",
    "protocol_error",
    "unmapped_dongle",
    "listener_shutdown",
    "read_error",
    "session_limit",
)
PENDING_DONGLE_STATUSES = ("pending", "rejected", "mapped")
TABLES = ("huawei_scada_power_samples", "huawei_scada_pending_dongles", "huawei_scada_sessions")


def upgrade() -> None:
    op.drop_constraint("ck_provider_connections_provider_code", "provider_connections", type_="check")
    op.create_check_constraint(
        "ck_provider_connections_provider_code",
        "provider_connections",
        f"provider_code IN {PROVIDER_CODES_AFTER!r}",
    )

    op.create_table(
        "huawei_scada_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Null until the banner arrives: a socket can be accepted, and can time
        # out, before the dongle has said who it is.
        sa.Column("dongle_serial", sa.String(length=120)),
        sa.Column("provider_connection_id", sa.Integer()),
        sa.Column("provider_mapping_id", sa.Integer()),
        sa.Column("asset_id", sa.Integer()),
        sa.Column("session_state", sa.String(length=32), nullable=False, server_default="connected"),
        sa.Column("close_reason", sa.String(length=32)),
        # A salted digest, never the address. Enough to see "the same source
        # reconnected"; useless for deciding which asset a dongle belongs to,
        # which is the point.
        sa.Column("peer_fingerprint", sa.String(length=64)),
        sa.Column("dongle_model", sa.String(length=120)),
        sa.Column("dongle_software_version", sa.String(length=120)),
        sa.Column("protocol_version", sa.String(length=32)),
        sa.Column("aggregate_unit_id", sa.Integer()),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_detail", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["provider_mapping_id"], ["asset_provider_mappings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.CheckConstraint(f"session_state IN {SESSION_STATES!r}", name="ck_huawei_scada_sessions_state"),
        sa.CheckConstraint(
            f"close_reason IS NULL OR close_reason IN {CLOSE_REASONS!r}",
            name="ck_huawei_scada_sessions_close_reason",
        ),
        sa.CheckConstraint("poll_count >= 0 AND sample_count >= 0 AND error_count >= 0", name="ck_huawei_scada_sessions_counters"),
        sa.CheckConstraint("last_seen_at >= opened_at", name="ck_huawei_scada_sessions_seen_after_open"),
    )
    op.create_index("ix_huawei_scada_sessions_dongle_time", "huawei_scada_sessions", ["dongle_serial", "opened_at"])
    op.create_index("ix_huawei_scada_sessions_open", "huawei_scada_sessions", ["session_state", "last_seen_at"])

    op.create_table(
        "huawei_scada_power_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("provider_mapping_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer()),
        sa.Column("dongle_serial", sa.String(length=120), nullable=False),
        sa.Column("source_sample_key", sa.String(length=255), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("supersedes_sample_id", sa.Integer()),
        # The protocol carries no timestamp of its own -- confirmed on both
        # pilots -- so this is when the listener read the block, and every row
        # says so in `metadata_json`.
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pv_input_power_kw", sa.Numeric(18, 6)),
        sa.Column("load_power_kw", sa.Numeric(18, 6)),
        sa.Column("grid_power_kw", sa.Numeric(18, 6)),
        sa.Column("battery_power_kw", sa.Numeric(18, 6)),
        sa.Column("total_active_power_kw", sa.Numeric(18, 6)),
        sa.Column("raw_registers_json", sa.JSON(), nullable=False),
        sa.Column("quality", sa.String(length=24), nullable=False, server_default="unknown"),
        sa.Column("completeness", sa.String(length=24), nullable=False, server_default="unknown"),
        sa.Column("session_state", sa.String(length=32), nullable=False, server_default="polling"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["provider_mapping_id"], ["asset_provider_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["huawei_scada_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supersedes_sample_id"], ["huawei_scada_power_samples.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"quality IN {QUALITY_STATES!r}", name="ck_huawei_scada_samples_quality"),
        sa.CheckConstraint(f"completeness IN {QUALITY_STATES!r}", name="ck_huawei_scada_samples_completeness"),
        sa.CheckConstraint(f"session_state IN {SESSION_STATES!r}", name="ck_huawei_scada_samples_session_state"),
        sa.CheckConstraint("source_revision >= 1", name="ck_huawei_scada_samples_revision"),
        # A sample with nothing in it is not evidence of anything; it must say
        # so in its quality rather than sit as five silent NULLs.
        sa.CheckConstraint(
            "quality = 'missing' OR pv_input_power_kw IS NOT NULL OR load_power_kw IS NOT NULL "
            "OR grid_power_kw IS NOT NULL OR battery_power_kw IS NOT NULL OR total_active_power_kw IS NOT NULL",
            name="ck_huawei_scada_samples_missing_value",
        ),
        sa.UniqueConstraint(
            "provider_mapping_id", "source_sample_key", "source_revision", name="uq_huawei_scada_sample_revision"
        ),
    )
    op.create_index("ix_huawei_scada_samples_asset_time", "huawei_scada_power_samples", ["asset_id", "observed_at"])
    op.create_index("ix_huawei_scada_samples_dongle_time", "huawei_scada_power_samples", ["dongle_serial", "observed_at"])
    op.create_index("ix_huawei_scada_samples_session", "huawei_scada_power_samples", ["session_id", "observed_at"])

    op.create_table(
        "huawei_scada_pending_dongles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dongle_serial", sa.String(length=120), nullable=False),
        sa.Column("provider_connection_id", sa.Integer()),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("peer_fingerprint", sa.String(length=64)),
        sa.Column("advertisement_json", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"], ondelete="SET NULL"),
        sa.CheckConstraint(f"status IN {PENDING_DONGLE_STATUSES!r}", name="ck_huawei_scada_pending_status"),
        sa.CheckConstraint("session_count >= 1", name="ck_huawei_scada_pending_session_count"),
        sa.UniqueConstraint("dongle_serial", name="uq_huawei_scada_pending_serial"),
    )
    op.create_index("ix_huawei_scada_pending_status", "huawei_scada_pending_dongles", ["status", "last_seen_at"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        existing = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if existing:
            raise RuntimeError(
                f"Refusing to downgrade: {table} holds {existing} row(s). "
                "Huawei SCADA evidence arrives from a logger in real time and cannot be re-collected."
            )
    connections = bind.execute(
        sa.text("SELECT count(*) FROM provider_connections WHERE provider_code = 'huawei_scada'")
    ).scalar_one()
    if connections:
        raise RuntimeError(
            f"Refusing to downgrade: {connections} huawei_scada provider connection(s) exist. "
            "Narrowing the provider constraint would leave rows the constraint forbids."
        )

    op.drop_index("ix_huawei_scada_pending_status", table_name="huawei_scada_pending_dongles")
    op.drop_table("huawei_scada_pending_dongles")
    op.drop_index("ix_huawei_scada_samples_session", table_name="huawei_scada_power_samples")
    op.drop_index("ix_huawei_scada_samples_dongle_time", table_name="huawei_scada_power_samples")
    op.drop_index("ix_huawei_scada_samples_asset_time", table_name="huawei_scada_power_samples")
    op.drop_table("huawei_scada_power_samples")
    op.drop_index("ix_huawei_scada_sessions_open", table_name="huawei_scada_sessions")
    op.drop_index("ix_huawei_scada_sessions_dongle_time", table_name="huawei_scada_sessions")
    op.drop_table("huawei_scada_sessions")

    op.drop_constraint("ck_provider_connections_provider_code", "provider_connections", type_="check")
    op.create_check_constraint(
        "ck_provider_connections_provider_code",
        "provider_connections",
        f"provider_code IN {PROVIDER_CODES_BEFORE!r}",
    )
