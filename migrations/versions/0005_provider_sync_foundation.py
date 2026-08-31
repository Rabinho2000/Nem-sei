"""Add provider health, sync control, canonical facts, and source policy.

Revision ID: 0005_provider_sync_foundation
Revises: 0004_asset_import_hardening
"""
from alembic import op
import sqlalchemy as sa


revision = "0005_provider_sync_foundation"
down_revision = "0004_asset_import_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_health",
        sa.Column("provider_connection_id", sa.Integer(), sa.ForeignKey("provider_connections.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("auth_state", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("access_state", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("provider_state", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("discovery_state", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("quota_state", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("sync_state", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_successful_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(48)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *[sa.CheckConstraint(f"{column} IN ('unknown', 'healthy', 'unavailable', 'degraded', 'not_configured')", name=f"ck_integration_health_{column.split('_')[0]}") for column in ("auth_state", "access_state", "provider_state", "discovery_state", "quota_state", "sync_state")],
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_connection_id", sa.Integer(), sa.ForeignKey("provider_connections.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_from", sa.DateTime(timezone=True)),
        sa.Column("requested_until", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("completeness", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(48)),
        sa.Column("safe_detail", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'running', 'success', 'partial', 'failed', 'deferred', 'rate_limited')", name="ck_sync_runs_status"),
    )
    op.create_index("ix_sync_runs_connection_capability", "sync_runs", ["provider_connection_id", "capability", "started_at"])
    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_connection_id", sa.Integer(), sa.ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("cursor_key", sa.String(120), nullable=False),
        sa.Column("checkpoint_json", sa.JSON(), nullable=False),
        sa.Column("covered_through", sa.DateTime(timezone=True)),
        sa.Column("last_successful_run_id", sa.Integer(), sa.ForeignKey("sync_runs.id", ondelete="SET NULL")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_connection_id", "capability", "cursor_key", name="uq_sync_cursors_scope"),
    )
    op.create_table(
        "provider_request_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_connection_id", sa.Integer(), sa.ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint_family", sa.String(80), nullable=False),
        sa.Column("quota_known", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True)),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("provider_retry_at", sa.DateTime(timezone=True)),
        sa.Column("actual_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_connection_id", "endpoint_family", name="uq_provider_request_state_scope"),
    )
    op.create_table(
        "provider_request_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_state_id", sa.Integer(), sa.ForeignKey("provider_request_states.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), sa.ForeignKey("sync_runs.id", ondelete="SET NULL")),
        sa.Column("purpose", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_after_at", sa.DateTime(timezone=True)),
        sa.Column("safe_detail", sa.Text()),
        sa.CheckConstraint("status IN ('reserved', 'called', 'succeeded', 'failed', 'deferred', 'rate_limited')", name="ck_provider_request_attempts_status"),
    )
    op.create_index("ix_provider_request_attempts_state_time", "provider_request_attempts", ["request_state_id", "occurred_at"])
    op.create_table(
        "asset_source_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider_mapping_id", sa.Integer(), sa.ForeignKey("asset_provider_mappings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_use", sa.String(24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_use IN ('monitoring', 'production')", name="ck_asset_source_policies_use"),
        sa.CheckConstraint("priority > 0", name="ck_asset_source_policies_priority"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_asset_source_policies_valid_range"),
    )
    op.create_index("ix_asset_source_policies_selection", "asset_source_policies", ["asset_id", "source_use", "valid_from", "valid_to"])
    op.execute("""
        INSERT INTO asset_source_policies (asset_id, provider_mapping_id, source_use, priority, is_fallback, valid_from, valid_to, created_at, updated_at)
        SELECT asset_id, id, 'monitoring', monitoring_priority, false, valid_from, valid_to, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM asset_provider_mappings WHERE monitoring_priority IS NOT NULL
    """)
    op.execute("""
        INSERT INTO asset_source_policies (asset_id, provider_mapping_id, source_use, priority, is_fallback, valid_from, valid_to, created_at, updated_at)
        SELECT asset_id, id, 'production', production_priority, false, valid_from, valid_to, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM asset_provider_mappings WHERE production_priority IS NOT NULL
    """)
    op.create_table(
        "monitoring_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider_mapping_id", sa.Integer(), sa.ForeignKey("asset_provider_mappings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), sa.ForeignKey("sync_runs.id", ondelete="SET NULL")),
        sa.Column("source_observation_key", sa.String(255), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_observation_id", sa.Integer(), sa.ForeignKey("monitoring_observations.id", ondelete="RESTRICT")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("condition", sa.String(24), nullable=False),
        sa.Column("freshness", sa.String(24), nullable=False),
        sa.Column("quality", sa.String(24), nullable=False),
        sa.Column("completeness", sa.String(24), nullable=False),
        sa.Column("raw_status_code", sa.String(120)), sa.Column("raw_status_text", sa.String(500)), sa.Column("safe_detail", sa.Text()), sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("condition IN ('operational', 'warning', 'fault', 'offline', 'unknown')", name="ck_monitoring_observations_condition"),
        sa.CheckConstraint("freshness IN ('fresh', 'stale', 'unknown')", name="ck_monitoring_observations_freshness"),
        sa.CheckConstraint("quality IN ('complete', 'partial', 'missing', 'invalid', 'unknown')", name="ck_monitoring_observations_quality"),
        sa.CheckConstraint("completeness IN ('complete', 'partial', 'missing', 'invalid', 'unknown')", name="ck_monitoring_observations_completeness"),
        sa.UniqueConstraint("provider_mapping_id", "source_observation_key", "source_revision", name="uq_monitoring_observation_revision"),
    )
    op.create_index("ix_monitoring_observations_asset_time", "monitoring_observations", ["asset_id", "observed_at"])
    op.create_table(
        "production_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider_mapping_id", sa.Integer(), sa.ForeignKey("asset_provider_mappings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), sa.ForeignKey("sync_runs.id", ondelete="SET NULL")),
        sa.Column("source_fact_key", sa.String(255), nullable=False), sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_fact_id", sa.Integer(), sa.ForeignKey("production_facts.id", ondelete="RESTRICT")),
        sa.Column("metric_kind", sa.String(64), nullable=False), sa.Column("period_start", sa.DateTime(timezone=True), nullable=False), sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granularity", sa.String(32), nullable=False), sa.Column("value", sa.Numeric(18, 6)), sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("quality", sa.String(24), nullable=False), sa.Column("completeness", sa.String(24), nullable=False), sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False), sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("metric_kind IN ('production_energy')", name="ck_production_facts_metric"),
        sa.CheckConstraint("quality IN ('complete', 'partial', 'missing', 'invalid', 'unknown')", name="ck_production_facts_quality"),
        sa.CheckConstraint("completeness IN ('complete', 'partial', 'missing', 'invalid', 'unknown')", name="ck_production_facts_completeness"),
        sa.CheckConstraint("value IS NOT NULL OR quality = 'missing'", name="ck_production_facts_missing_value"),
        sa.UniqueConstraint("provider_mapping_id", "source_fact_key", "source_revision", name="uq_production_fact_revision"),
    )
    op.create_index("ix_production_facts_asset_period", "production_facts", ["asset_id", "period_start", "period_end"])
    op.execute("""CREATE FUNCTION canonical_facts_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'canonical facts are append-only'; END; $$""")
    for table in ("monitoring_observations", "production_facts"):
        op.execute(f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION canonical_facts_immutable()")
        op.execute(f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION canonical_facts_immutable()")


def downgrade() -> None:
    for table in ("production_facts", "monitoring_observations"):
        op.execute(f"DROP TRIGGER {table}_no_delete ON {table}")
        op.execute(f"DROP TRIGGER {table}_no_update ON {table}")
    op.execute("DROP FUNCTION canonical_facts_immutable()")
    op.drop_index("ix_production_facts_asset_period", table_name="production_facts")
    op.drop_table("production_facts")
    op.drop_index("ix_monitoring_observations_asset_time", table_name="monitoring_observations")
    op.drop_table("monitoring_observations")
    op.drop_index("ix_asset_source_policies_selection", table_name="asset_source_policies")
    op.drop_table("asset_source_policies")
    op.drop_index("ix_provider_request_attempts_state_time", table_name="provider_request_attempts")
    op.drop_table("provider_request_attempts")
    op.drop_table("provider_request_states")
    op.drop_table("sync_cursors")
    op.drop_index("ix_sync_runs_connection_capability", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_table("integration_health")
