"""Notification infrastructure (D3): incident -> policy -> event -> channel.

`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md` -- a `DiagnosticIncident`
(D1) never knows a notification exists. This module is the layer in between:
`NotificationPolicy` decides *whether* an incident's episode is worth
telling anyone about, `NotificationEvent` is the durable, auditable record
of that decision (and, separately, of its delivery), and
`NotificationChannel` is where a decision would be delivered -- Telegram is
one `kind` of channel, not baked into any of this.

Three tables, no code path from `diagnostic_incidents` to Telegram that
skips a `NotificationEvent` row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


def _sql_in_list(values: tuple[str, ...]) -> str:
    """A tuple's `!r` repr is only safe to splice into `IN (...)` when it has
    two or more elements -- Python renders a one-element tuple as `('x',)`,
    trailing comma included, which Postgres rejects as a syntax error right
    before the closing paren. `CHANNEL_KINDS` has exactly one value today.
    """
    return ", ".join(repr(value) for value in values)


CHANNEL_KINDS = ("telegram",)

NOTIFICATION_EVENT_KINDS = ("opened", "escalated", "resolved")
NOTIFICATION_EVENT_STATUSES = ("pending", "sent", "failed", "skipped")
# "Warning" or better is worth a human's attention; "info" is dashboard-only
# by default. Kept as a real, enforced field -- unlike V1's own
# `MINIMUM_ALERT_SEVERITY`, which was stored in `alert_settings` and never
# once read by any alert decision. See DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md.
NOTIFICATION_SEVERITIES = ("critical", "warning", "info")


class NotificationChannel(Base):
    """Where a decided notification would be delivered.

    D3 deliberately ships with no real delivery implementation for any
    `kind` -- `notifications/telegram_client.py` only has an interface and a
    mock. `enabled` is a real, structural kill switch either way: disabled
    means the delivery step never calls a client at all, mock or otherwise.
    """

    __tablename__ = "notification_channels"
    __table_args__ = (CheckConstraint(f"kind IN ({_sql_in_list(CHANNEL_KINDS)})", name="ck_notification_channels_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Telegram-specific target; nullable because a future channel kind might
    # not need one, and because a disabled channel can exist unconfigured.
    target_chat_id: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationPolicy(Base):
    """Whether an incident's episode is notification-worthy, and when.

    A policy targets a scope (`min_severity`, optionally narrowed further by
    `rule_codes_json`) and decides which of an incident's real lifecycle
    moments (`notify_on_open`/`notify_on_resolve`/`escalation_after_minutes`)
    are worth telling `channel_id` about. It never touches `findings.py`'s
    logic or `DiagnosticIncident`'s own state machine -- it only reads them.

    `baseline_at`, when set, is the same mechanism V1 already proved
    necessary (`ALERT_BASELINE_AT`/`IGNORE_HISTORICAL_ALERTS`,
    `DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`'s V1 audit): an incident whose
    `opened_at` predates the baseline is not a *new* problem this policy
    should announce, it is pre-existing history the policy happened to be
    turned on after. `notify_on_resolve` is exempt from the baseline check
    on purpose -- a genuinely old problem finally clearing is worth saying
    regardless of when the policy started watching it.
    """

    __tablename__ = "notification_policies"
    __table_args__ = (
        CheckConstraint(f"min_severity IN {NOTIFICATION_SEVERITIES!r}", name="ck_notification_policies_min_severity"),
        CheckConstraint(
            "escalation_after_minutes IS NULL OR escalation_after_minutes > 0",
            name="ck_notification_policies_escalation_positive",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("notification_channels.id", ondelete="RESTRICT"), nullable=False)
    min_severity: Mapped[str] = mapped_column(String(24), nullable=False, default="warning")
    # NULL/empty means "every rule_code diagnostics/findings.py can produce" --
    # not the same as an empty list meaning "none", which would make the
    # policy silently notify nothing and look broken rather than unscoped.
    rule_codes_json: Mapped[list[str] | None] = mapped_column(JSON)
    notify_on_open: Mapped[bool] = mapped_column(nullable=False, default=True)
    notify_on_resolve: Mapped[bool] = mapped_column(nullable=False, default=True)
    escalation_after_minutes: Mapped[int | None] = mapped_column(Integer)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationEvent(Base):
    """One durable, auditable decision -- and, separately, its delivery.

    Identity is structural, never message text:
    `(incident_id, kind, channel_id)` is unique, so at most one `opened`,
    one `escalated`, and one `resolved` event can ever exist for one
    incident on one channel -- re-evaluating the same still-open incident a
    thousand times creates nothing new, and a resolved episode reopening
    later is a *new* `DiagnosticIncident` row (D1), so it is automatically a
    new, distinct notification identity too, with nothing here to reset.

    `status` covers exactly two independent questions without conflating
    them: was a notification decided (`pending`/`skipped` at decision time),
    and separately, did delivery succeed (`sent`/`failed`, only reachable
    from `pending`). `skipped` is a real, recorded decision (channel
    disabled, policy disabled at decision time), not a silent no-op --
    still created exactly once per identity, so it costs nothing to keep
    that decision auditable.
    """

    __tablename__ = "notification_events"
    __table_args__ = (
        CheckConstraint(f"kind IN {NOTIFICATION_EVENT_KINDS!r}", name="ck_notification_events_kind"),
        CheckConstraint(f"status IN {NOTIFICATION_EVENT_STATUSES!r}", name="ck_notification_events_status"),
        CheckConstraint("attempt_count >= 0", name="ck_notification_events_attempt_count"),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="ck_notification_events_sent_at"),
        UniqueConstraint("incident_id", "kind", "channel_id", name="uq_notification_events_identity"),
        Index("ix_notification_events_status", "status", "decided_at"),
        Index("ix_notification_events_incident", "incident_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_incidents.id", ondelete="RESTRICT"), nullable=False)
    # Provenance: which policy decided this -- not part of the identity (two
    # policies must not be able to double-notify the same channel about the
    # same incident episode; the channel is what a human actually receives).
    policy_id: Mapped[int] = mapped_column(ForeignKey("notification_policies.id", ondelete="RESTRICT"), nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("notification_channels.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    skipped_reason: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # A snapshot of the incident at decision time (rule_code, severity,
    # asset/device labels, duration for a `resolved` kind) -- for audit and
    # for rendering `message`, never re-derived from a later, possibly
    # different incident state.
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
