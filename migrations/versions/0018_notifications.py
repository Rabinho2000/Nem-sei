"""Notification infrastructure: D3, channel -> policy -> event.

Studied and scoped in `docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`.
`diagnostic_incidents` (D1) is unchanged and unaware this exists -- these
three tables sit strictly downstream of it. No real Telegram client exists
yet (`notifications/telegram_client.py` ships only an interface and a mock
this migration); `notification_channels.enabled` defaults to `False`, so
even once this migration lands, nothing here can deliver anything until a
human explicitly enables a channel.

Revision ID: 0018_notifications
Revises: 0017_diagnostic_incidents
"""
from alembic import op
import sqlalchemy as sa


revision = "0018_notifications"
down_revision = "0017_diagnostic_incidents"
branch_labels = None
depends_on = None


def _sql_in_list(values: tuple[str, ...]) -> str:
    """A one-element tuple's `!r` repr keeps its trailing comma (`('x',)`),
    which Postgres rejects inside `IN (...)`. `CHANNEL_KINDS` has exactly
    one value today -- this keeps the constraint valid regardless.
    """
    return ", ".join(repr(value) for value in values)


CHANNEL_KINDS = ("telegram",)
NOTIFICATION_EVENT_KINDS = ("opened", "escalated", "resolved")
NOTIFICATION_EVENT_STATUSES = ("pending", "sent", "failed", "skipped")
NOTIFICATION_SEVERITIES = ("critical", "warning", "info")


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("target_chat_id", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"kind IN ({_sql_in_list(CHANNEL_KINDS)})", name="ck_notification_channels_kind"),
    )

    op.create_table(
        "notification_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("min_severity", sa.String(length=24), nullable=False, server_default="warning"),
        sa.Column("rule_codes_json", sa.JSON()),
        sa.Column("notify_on_open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_on_resolve", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("escalation_after_minutes", sa.Integer()),
        sa.Column("baseline_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"min_severity IN {NOTIFICATION_SEVERITIES!r}", name="ck_notification_policies_min_severity"),
        sa.CheckConstraint(
            "escalation_after_minutes IS NULL OR escalation_after_minutes > 0",
            name="ck_notification_policies_escalation_positive",
        ),
    )

    op.create_table(
        "notification_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("skipped_reason", sa.Text()),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["diagnostic_incidents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["policy_id"], ["notification_policies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"kind IN {NOTIFICATION_EVENT_KINDS!r}", name="ck_notification_events_kind"),
        sa.CheckConstraint(f"status IN {NOTIFICATION_EVENT_STATUSES!r}", name="ck_notification_events_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notification_events_attempt_count"),
        sa.CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="ck_notification_events_sent_at"),
        sa.UniqueConstraint("incident_id", "kind", "channel_id", name="uq_notification_events_identity"),
    )
    op.create_index("ix_notification_events_status", "notification_events", ["status", "decided_at"])
    op.create_index("ix_notification_events_incident", "notification_events", ["incident_id"])


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM notification_events")).scalar_one()
    if existing:
        raise RuntimeError(f"Refusing to downgrade: {existing} notification events are auditable history, not disposable.")
    op.drop_index("ix_notification_events_incident", table_name="notification_events")
    op.drop_index("ix_notification_events_status", table_name="notification_events")
    op.drop_table("notification_events")
    op.drop_table("notification_policies")
    op.drop_table("notification_channels")
