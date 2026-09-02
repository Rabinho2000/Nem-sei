"""Grouped recovery digest (Telegram O&M redesign, req 13/17).

Every test proves one of the concrete rules: never a recovery for an episode
that was never notified, a critical/already-reminded recovery goes out
immediately instead of into this digest (never both), only O&M-active
installations, and the digest window chains like D6's diagnostics digest.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.service import create_asset
from nemsei.contracts.service import set_service_contract
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.digests import build_recovery_digest_payload, generate_digest, render_recovery_digest_text
from nemsei.notifications.models import NotificationEpisode


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int = 9, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def make_om_asset(session, *, name: str = "DIACO") -> int:
    asset = create_asset(session, canonical_name=name)
    set_service_contract(session, asset_id=asset.id, created_by="ops", valid_from=date(2020, 1, 1), valid_to=None, service_kind="om")
    return asset.id


def make_incident(session, *, asset_id: int, opened_at: datetime, resolved_at: datetime, rule_code: str = "plant_offline", severity: str = "warning") -> DiagnosticIncident:
    incident = DiagnosticIncident(
        rule_code=rule_code, asset_id=asset_id, device_id=None, severity=severity, status="resolved",
        opened_at=opened_at, last_observed_at=resolved_at, resolved_at=resolved_at,
        occurrence_count=1, detector_version="2", evidence_json={}, created_at=opened_at, updated_at=resolved_at,
    )
    session.add(incident)
    session.flush()
    return incident


def make_closed_episode(
    session, *, asset_id: int, opened_at: datetime, closed_at: datetime, incident_id: int,
    severity_peak: str = "warning", notified_at: datetime | None = None, reminder_count: int = 0,
    problem_family: str = "communication",
) -> NotificationEpisode:
    episode = NotificationEpisode(
        asset_id=asset_id, device_id=None, problem_family=problem_family, status="closed",
        severity_peak=severity_peak, opened_at=opened_at, last_activity_at=closed_at, closed_at=closed_at,
        flap_count=1, first_incident_id=incident_id, last_incident_id=incident_id, eligible_at=opened_at,
        notified_at=notified_at, reminder_count=reminder_count, recovery_notified=False,
        created_at=opened_at, updated_at=closed_at,
    )
    session.add(episode)
    session.flush()
    return episode


# --- the hard floor: never a never-notified recovery ----------------------------


def test_a_never_notified_recovery_is_absent_from_the_digest(factory) -> None:
    with factory() as session, session.begin():
        asset_id = make_om_asset(session)
        incident = make_incident(session, asset_id=asset_id, opened_at=utc(9), resolved_at=utc(9, 20))
        make_closed_episode(session, asset_id=asset_id, opened_at=utc(9), closed_at=utc(9, 20), incident_id=incident.id, notified_at=None)

    with factory() as session:
        payload = build_recovery_digest_payload(session, window_start=utc(8), window_end=utc(12))
    assert payload["recoveries"] == []


# --- a critical/already-notified recovery never shows up here (goes immediate) --


def test_a_critical_notified_recovery_is_excluded_it_already_went_out_immediately(factory) -> None:
    with factory() as session, session.begin():
        asset_id = make_om_asset(session)
        incident = make_incident(session, asset_id=asset_id, opened_at=utc(9), resolved_at=utc(11), severity="critical")
        make_closed_episode(
            session, asset_id=asset_id, opened_at=utc(9), closed_at=utc(11), incident_id=incident.id,
            severity_peak="critical", notified_at=utc(9, 31),
        )

    with factory() as session:
        payload = build_recovery_digest_payload(session, window_start=utc(8), window_end=utc(12))
    assert payload["recoveries"] == []


# --- a notified, non-critical, short recovery groups into the digest -----------


def test_a_notified_non_critical_short_recovery_appears_in_the_digest(factory) -> None:
    with factory() as session, session.begin():
        asset_id = make_om_asset(session, name="FC Alverca")
        incident = make_incident(session, asset_id=asset_id, opened_at=utc(9), resolved_at=utc(9, 31))
        make_closed_episode(
            session, asset_id=asset_id, opened_at=utc(9), closed_at=utc(9, 31), incident_id=incident.id,
            severity_peak="warning", notified_at=utc(9, 5),
        )

    with factory() as session:
        payload = build_recovery_digest_payload(session, window_start=utc(8), window_end=utc(12))

    assert len(payload["recoveries"]) == 1
    assert payload["recoveries"][0]["name"] == "FC Alverca"
    assert payload["recoveries"][0]["duration_minutes"] == 31
    text = render_recovery_digest_text(payload)
    assert "FC Alverca" in text and "31min" in text
    assert "recuperaram" in text


def test_an_empty_window_renders_explicitly_not_silently(factory) -> None:
    with factory() as session:
        payload = build_recovery_digest_payload(session, window_start=utc(8), window_end=utc(12))
    assert payload["recoveries"] == []
    text = render_recovery_digest_text(payload)
    assert "Nenhuma recuperação" in text


# --- only O&M-active installations (req 1) --------------------------------------


def test_a_recovery_outside_om_scope_never_appears(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="No O&M")  # no service contract at all
        incident = make_incident(session, asset_id=asset.id, opened_at=utc(9), resolved_at=utc(9, 31))
        make_closed_episode(
            session, asset_id=asset.id, opened_at=utc(9), closed_at=utc(9, 31), incident_id=incident.id,
            notified_at=utc(9, 5),
        )

    with factory() as session:
        payload = build_recovery_digest_payload(session, window_start=utc(8), window_end=utc(12))
    assert payload["recoveries"] == []


# --- window chaining, like D6's diagnostics digest ------------------------------


def test_recovery_digest_windows_chain_since_the_last_one(factory) -> None:
    with factory() as session, session.begin():
        asset_id = make_om_asset(session)
        # Closes well before the first digest's own bootstrapped window
        # ([6:00, 8:00), interval=120min) -- must never appear anywhere.
        incident = make_incident(session, asset_id=asset_id, opened_at=utc(4), resolved_at=utc(5, 30))
        make_closed_episode(
            session, asset_id=asset_id, opened_at=utc(4), closed_at=utc(5, 30), incident_id=incident.id,
            notified_at=utc(4, 5),
        )

    with factory() as session, session.begin():
        first = generate_digest(session, window_end=utc(8), interval_minutes=120, kind="recoveries")
    assert first.window_start == utc(6)  # bootstrapped: window_end - interval, first digest of this kind
    assert first.summary_json["recoveries"] == []

    with factory() as session, session.begin():
        second = generate_digest(session, window_end=utc(10), interval_minutes=120, kind="recoveries")
    assert second.window_start == utc(8)  # chained from the first digest's own window_end, not a fresh guess
    assert second.summary_json["recoveries"] == []

    with factory() as session, session.begin():
        incident2 = make_incident(session, asset_id=asset_id, opened_at=utc(10, 30), resolved_at=utc(11))
        make_closed_episode(
            session, asset_id=asset_id, opened_at=utc(10, 30), closed_at=utc(11), incident_id=incident2.id,
            notified_at=utc(10, 35),
        )
        third = generate_digest(session, window_end=utc(12), interval_minutes=120, kind="recoveries")
    assert third.window_start == utc(10)
    assert len(third.summary_json["recoveries"]) == 1
