"""Migration `0035_notification_episodes`'s one-time backfill, against real
pre-`0035` data -- not the empty-database path every other test exercises
(production's `notification_events` count was 0 on 2026-08-23, so the ORM-
level tests in `test_notification_episodes.py` never seed a row here before
upgrading, and never would have caught this).

Real bug caught by the deployment contract check, not hypothetical:
`diagnostic_incidents.status` speaks `open`/`resolved`;
`notification_episodes.status` speaks `open`/`closed`. The first version of
`_backfill_episodes` passed the incident's own value straight through, which
wrote `status='resolved'` alongside a real `closed_at` -- violating
`ck_notification_episodes_closed_at` (`(status = 'closed') = (closed_at IS
NOT NULL)`) the moment a resolved incident with a real notification event
ever existed to backfill.
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def utc(hour: int = 9, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_backfill_maps_a_resolved_incident_to_a_closed_episode(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    config = Config("alembic.ini")

    # Stop one revision short of the migration under test, so the schema at
    # insert time matches what `_backfill_episodes` actually runs against --
    # no `episode_id`/`notification_episodes` yet.
    command.upgrade(config, "0034_installation_contacts")

    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        asset_id = conn.execute(
            text(
                "INSERT INTO assets (public_id, canonical_name, normalized_name, lifecycle_status, "
                "timezone_source, review_status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'DIACO', 'diaco', 'unknown', 'manual', 'clear', :now, :now) RETURNING id"
            ),
            {"now": utc(9)},
        ).scalar_one()
        incident_id = conn.execute(
            text(
                "INSERT INTO diagnostic_incidents (rule_code, asset_id, severity, status, opened_at, "
                "last_observed_at, resolved_at, occurrence_count, detector_version, evidence_json, "
                "created_at, updated_at) "
                "VALUES ('plant_offline', :asset_id, 'critical', 'resolved', :opened_at, :resolved_at, "
                ":resolved_at, 1, '2', '{}', :opened_at, :resolved_at) RETURNING id"
            ),
            {"asset_id": asset_id, "opened_at": utc(9), "resolved_at": utc(11)},
        ).scalar_one()
        channel_id = conn.execute(
            text(
                "INSERT INTO notification_channels (name, kind, enabled, created_at, updated_at) "
                "VALUES ('Ops', 'telegram', false, :now, :now) RETURNING id"
            ),
            {"now": utc(9)},
        ).scalar_one()
        policy_id = conn.execute(
            text(
                "INSERT INTO notification_policies (name, enabled, channel_id, min_severity, asset_scope, "
                "notify_on_open, notify_on_resolve, created_at, updated_at) "
                "VALUES ('A', true, :channel_id, 'critical', 'all', true, true, :now, :now) RETURNING id"
            ),
            {"channel_id": channel_id, "now": utc(9)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO notification_events (incident_id, policy_id, channel_id, kind, status, "
                "decided_at, attempt_count, message, evidence_json, sent_at, created_at, updated_at) "
                "VALUES (:incident_id, :policy_id, :channel_id, 'opened', 'sent', :decided_at, 1, 'x', '{}', "
                ":sent_at, :decided_at, :decided_at)"
            ),
            {"incident_id": incident_id, "policy_id": policy_id, "channel_id": channel_id,
             "decided_at": utc(9, 5), "sent_at": utc(9, 5)},
        )

    command.upgrade(config, "0035_notification_episodes")

    with engine.begin() as conn:
        episode = conn.execute(
            text("SELECT status, closed_at, notified_at FROM notification_episodes WHERE first_incident_id = :id"),
            {"id": incident_id},
        ).mappings().one()
    assert episode["status"] == "closed"
    assert episode["closed_at"] is not None
    assert episode["notified_at"] is not None  # the real 'sent' opened event was found
