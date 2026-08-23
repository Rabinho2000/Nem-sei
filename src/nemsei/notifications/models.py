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
    turned on after -- unless it later changes in a way that would put it
    in scope for the first time, which `NotificationBaselineSnapshot`
    exists to detect (`opened_at` alone cannot). `notify_on_resolve` never
    needs a baseline check itself -- it is already gated on a prior
    `opened`/`escalated` event existing, which a pre-existing, unchanged
    incident never earns.
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


class NotificationBaselineSnapshot(Base):
    """Closes the one gap `baseline_at` alone cannot: `opened_at` says
    *when* an incident started, never *what it looked like* at that time, so
    a pre-existing incident that later worsens into a different policy's
    scope was, until this table existed, indistinguishable from one that
    never changed -- both just "opened_at < baseline_at", forever excluded.

    The first time a policy evaluates a pre-existing incident (`opened_at <
    baseline_at`) at or after its own `baseline_at`, it captures that
    incident's severity *right then* as the closest available proxy for
    "what this looked like when we started watching" -- `DiagnosticIncident`
    keeps no severity history, so this is the earliest point one can be
    recorded. Captured once, never overwritten: it is a fixed historical
    record, not a moving target, which is what makes "did this change since
    baseline" a stable, auditable question instead of a re-derived guess.

    A later evaluation compares the *current* incident scope against this
    frozen snapshot, not against `opened_at`: still in scope at capture time
    means nothing has changed since baseline (skip, forever, same as
    today); no longer matching at capture time but matching now means a
    genuine post-baseline transition (a warning that became critical, most
    commonly) -- worth exactly one notification, no different in kind from
    a brand-new incident reaching the same scope for the first time.

    One row per (incident, policy): a snapshot is relative to what a
    *specific* policy's scope considered "in" at baseline, not a global
    property of the incident.
    """

    __tablename__ = "notification_baseline_snapshots"
    __table_args__ = (
        UniqueConstraint("incident_id", "policy_id", name="uq_notification_baseline_snapshots_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_incidents.id", ondelete="RESTRICT"), nullable=False)
    policy_id: Mapped[int] = mapped_column(ForeignKey("notification_policies.id", ondelete="RESTRICT"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severity_at_capture: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


DIGEST_DELIVERY_STATUSES = ("pending", "delivered", "failed")


class DigestRun(Base):
    """One periodic diagnostic digest -- a *summary* of incidents over a
    window, never a second finding or a second incident (D6,
    `docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`).

    Deliberately one table, not two: unlike `NotificationEvent` (one row per
    incident per channel, because many incidents can each need their own
    decision), a digest is one summary of everything, delivered at most once
    to at most one channel per run -- there is no per-item identity to track
    separately, so decision and delivery share this row instead of a second
    `DigestEvent` table that would only ever have one child per parent.

    `window_start`/`window_end` are the same values, in a job's own
    `payload_json["scheduled_for"]`, that
    `JobRepository.enqueue_due_digest_generation` already persists in
    `ScheduleState` -- not a fresh `now()` read at generation time. This is
    what makes the unique constraint below actually catch a real race: two
    concurrent attempts for the *same due slot* compute the identical
    `window_end`, not two almost-but-not-quite-equal timestamps a millisecond
    apart that would never collide.
    """

    __tablename__ = "digest_runs"
    __table_args__ = (
        CheckConstraint(f"delivery_status IN {DIGEST_DELIVERY_STATUSES!r}", name="ck_digest_runs_delivery_status"),
        CheckConstraint("window_end > window_start", name="ck_digest_runs_window"),
        CheckConstraint("(delivery_status = 'delivered') = (delivered_at IS NOT NULL)", name="ck_digest_runs_delivered_at"),
        CheckConstraint("delivery_attempt_count >= 0", name="ck_digest_runs_attempt_count"),
        UniqueConstraint("window_start", "window_end", name="uq_digest_runs_window"),
        Index("ix_digest_runs_window_end", "window_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Structured content -- per-portfolio breakdown, top installations, the
    # new/persistent/resolved split -- everything `rendered_text` was built
    # from, kept separately so a future renderer (or a real Telegram
    # message format) can reuse the same numbers without re-querying
    # `diagnostic_incidents` for a window that has already closed.
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Delivery: optional and inert by default. D6 ships no real Telegram
    # client, same structural guarantee as D3 -- `channel_id` nullable means
    # a digest can be generated and rendered with no delivery attempted at
    # all, which is exactly today's state (no channel configured).
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("notification_channels.id", ondelete="RESTRICT"))
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    delivery_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
