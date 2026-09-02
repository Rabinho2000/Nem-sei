"""NotificationEpisode: the identity a flapping problem keeps.

Telegram O&M redesign (see the session's plan). The gap this closes: D3's
`NotificationEvent` identity is `(incident_id, kind, channel_id)`, and D1's
`DiagnosticIncident` closes and reopens a brand-new row every time a
`plant_offline`-class condition flaps (observed in real data: installations
cycling offline/online every ~16 minutes). Each reopening is, by D1's own
correct design, a genuinely new `incident_id` -- so D3's identity, unchanged,
would notify once per flap. That is the storm this migration exists to stop.

`NotificationEpisode` sits between the two: one row per
`(asset_id, problem_family, device_id)` while the underlying condition stays
"the same problem" -- including through a short reopen (merged, `flap_count`
incremented) rather than only through one uninterrupted `DiagnosticIncident`.
`notification_events.episode_id` becomes the real notification identity;
`incident_id` stays, now meaning "the most recent underlying incident", kept
for evidence/audit, not for dedup.

Nothing in `diagnostic_incidents` changes -- D1's lifecycle, tested and LIVE
VERIFIED, is untouched. This is additive, plus one column and one constraint
change on `notification_events`, whose production row count was confirmed 0
on 2026-08-23; the backfill below is written to be correct for a non-zero
count too, using each incident's own real evidence, never a guess.

Revision ID: 0035_notification_episodes
Revises: 0034_installation_contacts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_notification_episodes"
down_revision = "0034_installation_contacts"
branch_labels = None
depends_on = None

EPISODE_STATUSES = ("open", "closed")
# `reminder` is new here -- req 3's "reminder após 4h/24h" is its own event
# kind, distinct from `escalated` (D3's one-shot, severity-driven escalation,
# unchanged). Unlike the other three kinds, more than one `reminder` row is
# legitimate for the same episode over its life (a 4h reminder, then a 24h
# reminder) -- so `reminder` is deliberately excluded from the structural
# uniqueness below; `NotificationEpisode.last_reminder_at`/`reminder_count`
# (updated transactionally alongside each reminder insert) is what prevents
# sending the *same* reminder twice, a policy decision in
# `notifications/eligibility.py`, not a DB identity.
NEW_NOTIFICATION_EVENT_KINDS = ("opened", "escalated", "resolved", "reminder")
# Mirrors `diagnostics.incident_categories.incident_category` (the shared
# classification `notifications/problem_families.py` also defers to) at the
# time this migration was written -- frozen here deliberately, like every
# other migration in this project, so this file stays a correct historical
# snapshot even if that module's mapping grows a new rule_code later. Only
# used for the one-time backfill below.
_PROBLEM_FAMILY_BY_RULE_CODE = {
    "plant_offline": "communication",
    "plant_fault": "fault",
    "plant_warning": "fault",
    "plant_state_stale": "coverage",
    "device_unavailable": "fault",
    "zero_power_while_peers_active": "fault",
    "power_disparity_among_peers": "fault",
    "daily_energy_disparity_among_peers": "fault",
    "zero_production_in_productive_window": "fault",
    "stale_reading": "coverage",
    "device_unknown_status": "coverage",
    "device_no_history": "coverage",
    "partial_device_coverage": "coverage",
}
PROBLEM_FAMILIES = ("communication", "fault", "coverage")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.create_table(
        "notification_episodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="RESTRICT")),
        sa.Column("problem_family", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("severity_peak", sa.String(length=24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("flap_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "first_incident_id", sa.Integer(), sa.ForeignKey("diagnostic_incidents.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "last_incident_id", sa.Integer(), sa.ForeignKey("diagnostic_incidents.id", ondelete="RESTRICT"), nullable=False
        ),
        # When the episode first crossed its notification-eligibility gate
        # (the 0-30min silence window) -- NULL until it does, distinct from
        # `opened_at` (when the underlying condition itself started).
        sa.Column("eligible_at", sa.DateTime(timezone=True)),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("last_reminder_at", sa.DateTime(timezone=True)),
        sa.Column("reminder_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recovery_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_check_constraint(
        "ck_notification_episodes_status", "notification_episodes", f"status IN ({_values(EPISODE_STATUSES)})"
    )
    op.create_check_constraint(
        "ck_notification_episodes_family", "notification_episodes", f"problem_family IN ({_values(PROBLEM_FAMILIES)})"
    )
    op.create_check_constraint(
        "ck_notification_episodes_closed_at", "notification_episodes", "(status = 'closed') = (closed_at IS NOT NULL)"
    )
    op.create_check_constraint("ck_notification_episodes_flap_count", "notification_episodes", "flap_count >= 1")
    op.create_check_constraint("ck_notification_episodes_reminder_count", "notification_episodes", "reminder_count >= 0")
    op.create_index("ix_notification_episodes_status", "notification_episodes", ["status", "last_activity_at"])
    op.create_index("ix_notification_episodes_asset", "notification_episodes", ["asset_id", "status"])
    # Same functional-index shape as `uq_diagnostic_incidents_open_identity`
    # (0017): only one *open* episode per identity, and NULL device_id (an
    # asset-level problem family) must not let two open rows coexist.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notification_episodes_open_identity
        ON notification_episodes (asset_id, problem_family, COALESCE(device_id, -1))
        WHERE status = 'open'
        """
    )

    # Reminders are an explicit opt-in per policy, not a behaviour every
    # existing policy suddenly gains: NULL (the default for every row that
    # exists today, both of the two real production policies included) means
    # "no reminder cadence", exactly what those policies do today. A
    # deployment that wants req 3's cadence sets this to e.g. `[240, 1440]`
    # deliberately -- same discipline as every other automatic behaviour in
    # this codebase (`Settings.notification_processing_enabled` and friends):
    # off until someone turns it on, never on by construction.
    op.add_column("notification_policies", sa.Column("reminder_minutes_json", sa.JSON()))

    op.add_column("notification_events", sa.Column("episode_id", sa.Integer()))
    _backfill_episodes()
    op.alter_column("notification_events", "episode_id", nullable=False)
    op.create_foreign_key(
        "fk_notification_events_episode", "notification_events", "notification_episodes", ["episode_id"], ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("ck_notification_events_kind", "notification_events", type_="check")
    op.create_check_constraint(
        "ck_notification_events_kind", "notification_events", f"kind IN ({_values(NEW_NOTIFICATION_EVENT_KINDS)})"
    )

    # The old blanket unique constraint keyed dedup on `incident_id`, which a
    # flap makes a moving target -- see the module docstring. Replaced with a
    # partial unique index keyed on `episode_id` instead, and deliberately
    # excluding `kind = 'reminder'` (more than one legitimate reminder per
    # episode -- see NEW_NOTIFICATION_EVENT_KINDS above).
    op.drop_constraint("uq_notification_events_identity", "notification_events", type_="unique")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notification_events_identity
        ON notification_events (episode_id, kind, channel_id)
        WHERE kind <> 'reminder'
        """
    )
    op.create_index("ix_notification_events_episode", "notification_events", ["episode_id"])


def _backfill_episodes() -> None:
    """One `NotificationEpisode` per incident already referenced by a
    `notification_events` row -- 1:1, no flap-merging applied retroactively
    (there is no way to know, after the fact, whether a pre-existing
    incident's reopening was a real flap or a genuinely new problem; treating
    each existing row as its own episode is the honest, non-inventive
    choice). `notified_at` is read from the incident's own earliest sent
    `opened`/`escalated` event, not assumed -- an incident that only ever
    produced a `skipped`/`failed` event backfills with `notified_at = NULL`.
    """
    bind = op.get_bind()
    incidents = bind.execute(
        sa.text(
            """
            SELECT di.id, di.asset_id, di.device_id, di.rule_code, di.severity, di.status,
                   di.opened_at, di.last_observed_at, di.resolved_at, di.updated_at,
                   (
                       SELECT MIN(ne.sent_at) FROM notification_events ne
                       WHERE ne.incident_id = di.id AND ne.kind IN ('opened', 'escalated') AND ne.sent_at IS NOT NULL
                   ) AS notified_at
            FROM diagnostic_incidents di
            WHERE di.id IN (SELECT DISTINCT incident_id FROM notification_events)
            """
        )
    ).mappings().all()

    for incident in incidents:
        family = _PROBLEM_FAMILY_BY_RULE_CODE.get(incident["rule_code"], "fault")
        # `diagnostic_incidents.status` speaks D1's vocabulary
        # (INCIDENT_STATUSES = "open"/"resolved"); `notification_episodes.status`
        # speaks its own (EPISODE_STATUSES = "open"/"closed"). Passing the
        # incident's value straight through wrote status="resolved" with
        # closed_at set, tripping ck_notification_episodes_closed_at, which
        # requires status="closed" whenever closed_at is not null.
        episode_status = "closed" if incident["status"] == "resolved" else incident["status"]
        episode_id = bind.execute(
            sa.text(
                """
                INSERT INTO notification_episodes (
                    asset_id, device_id, problem_family, status, severity_peak,
                    opened_at, last_activity_at, closed_at, flap_count,
                    first_incident_id, last_incident_id, eligible_at, notified_at,
                    reminder_count, recovery_notified, created_at, updated_at
                ) VALUES (
                    :asset_id, :device_id, :family, :status, :severity,
                    :opened_at, :last_activity_at, :closed_at, 1,
                    :incident_id, :incident_id, :opened_at, :notified_at,
                    0, false, :opened_at, :updated_at
                )
                RETURNING id
                """
            ),
            {
                "asset_id": incident["asset_id"],
                "device_id": incident["device_id"],
                "family": family,
                "status": episode_status,
                "severity": incident["severity"],
                "opened_at": incident["opened_at"],
                "last_activity_at": incident["last_observed_at"],
                "closed_at": incident["resolved_at"],
                "incident_id": incident["id"],
                "notified_at": incident["notified_at"],
                "updated_at": incident["updated_at"],
            },
        ).scalar_one()
        bind.execute(
            sa.text("UPDATE notification_events SET episode_id = :episode_id WHERE incident_id = :incident_id"),
            {"episode_id": episode_id, "incident_id": incident["id"]},
        )


def downgrade() -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM notification_episodes")).scalar_one()
    if existing:
        raise RuntimeError(f"Refusing to downgrade: {existing} notification episodes are operational history, not disposable.")
    reminders = op.get_bind().execute(
        sa.text("SELECT count(*) FROM notification_events WHERE kind = 'reminder'")
    ).scalar_one()
    if reminders:
        raise RuntimeError(f"Refusing to downgrade: {reminders} reminder events have no home in the old schema.")
    op.drop_index("ix_notification_events_episode", table_name="notification_events")
    op.execute("DROP INDEX IF EXISTS uq_notification_events_identity")
    op.create_unique_constraint(
        "uq_notification_events_identity", "notification_events", ["incident_id", "kind", "channel_id"]
    )
    op.drop_constraint("ck_notification_events_kind", "notification_events", type_="check")
    op.create_check_constraint(
        "ck_notification_events_kind", "notification_events", f"kind IN ({_values(('opened', 'escalated', 'resolved'))})"
    )
    op.drop_constraint("fk_notification_events_episode", "notification_events", type_="foreignkey")
    op.drop_column("notification_events", "episode_id")
    op.drop_column("notification_policies", "reminder_minutes_json")
    op.execute("DROP INDEX IF EXISTS uq_notification_episodes_open_identity")
    op.drop_index("ix_notification_episodes_asset", table_name="notification_episodes")
    op.drop_index("ix_notification_episodes_status", table_name="notification_episodes")
    op.drop_table("notification_episodes")
