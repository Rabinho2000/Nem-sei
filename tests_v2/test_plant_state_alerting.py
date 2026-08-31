"""Plant-level state: the read, the rules, and the incident that alerts on it.

This is the chain V1 had and V2 did not: read what the provider says about the
installation, turn a fall into an incident, let the existing notification
policies carry it to Telegram, and close it when the plant comes back.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.findings import evaluate_plant_findings
from nemsei.diagnostics.incidents import evaluate_and_persist_incidents
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.jobs.repository import JobRepository
from nemsei.monitoring.models import MonitoringObservation
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.service import create_connection


NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)


def ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


def plant(condition: str | None, observed_at: datetime | None, now: datetime = NOW):
    return evaluate_plant_findings(asset_id=1, condition=condition, observed_at=observed_at, now=now)


# --- the rules ----------------------------------------------------------------


def test_an_installation_never_read_produces_no_finding():
    """Never connected is an onboarding gap, not an outage to wake someone for."""
    assert plant(None, None) == []


@pytest.mark.parametrize(
    ("condition", "rule_code", "severity"),
    [("offline", "plant_offline", "critical"), ("fault", "plant_fault", "critical"), ("warning", "plant_warning", "warning")],
)
def test_a_provider_reported_problem_becomes_a_finding(condition, rule_code, severity):
    findings = plant(condition, ago(minutes=5))
    assert [(finding.rule_code, finding.severity) for finding in findings] == [(rule_code, severity)]
    assert findings[0].active_since == ago(minutes=5)


def test_a_working_installation_produces_no_finding():
    assert plant("operational", ago(minutes=5)) == []


def test_an_unknown_condition_is_not_an_incident():
    """Sigenergy every night: a complete payload the provider states no status in.

    "We do not know" is not "something is wrong", and making it an incident
    would mean a nightly warning for every battery plant on the account.
    """
    assert plant("unknown", ago(minutes=5)) == []


def test_a_reading_that_stopped_is_its_own_warning():
    findings = plant("operational", ago(hours=40))
    assert [finding.rule_code for finding in findings] == ["plant_state_stale"]
    assert findings[0].severity == "warning"
    # Stale since the moment it went stale, not since the reading itself.
    assert findings[0].active_since == ago(hours=40) + timedelta(hours=24)


def test_an_unchanged_plant_re_read_every_15_minutes_is_not_stale():
    """The bug this test exists for, found live in production.

    `monitoring_observations` gets a new row only when the evidence *changes*,
    so a plant operational since yesterday keeps yesterday's row while being
    successfully re-read every 15 minutes. Judging staleness by the observation
    reported every healthy, unchanging plant as unread within a day.
    """
    findings = evaluate_plant_findings(
        asset_id=1, condition="operational", observed_at=ago(days=3), confirmed_at=ago(minutes=6), now=NOW
    )
    assert findings == []


def test_a_confirmation_that_stopped_is_still_stale():
    """The other half: reads really did stop, whatever the observation says."""
    findings = evaluate_plant_findings(
        asset_id=1, condition="operational", observed_at=ago(days=3), confirmed_at=ago(hours=40), now=NOW
    )
    assert [finding.rule_code for finding in findings] == ["plant_state_stale"]
    assert findings[0].evidence["confirmed_at"] == ago(hours=40).isoformat()


def test_staleness_wins_over_the_condition_it_is_too_old_to_vouch_for():
    """An offline reading from last month must not alert as an outage now."""
    assert [finding.rule_code for finding in plant("offline", ago(days=30))] == ["plant_state_stale"]


# --- against the database -----------------------------------------------------


@pytest.fixture
def factory(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    return build_session_factory(create_engine(settings.database_url))


def observed_plant(session, *, asset_id: int, condition: str, observed_at: datetime, key: str) -> None:
    connection = session.scalar(select(ProviderConnection))
    if connection is None:
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="test-plant-state",
            display_name="test", credential_reference="primary", enabled=True,
            configuration_status="configured",
        )
        session.flush()
    mapping = session.scalar(select(AssetProviderMapping).where(AssetProviderMapping.asset_id == asset_id))
    if mapping is None:
        mapping = AssetProviderMapping(
            asset_id=asset_id, provider_connection_id=connection.id, resource_kind="plant",
            external_id=f"NE={asset_id}", normalized_external_id=f"ne={asset_id}", mapping_status="active",
            valid_from=date(2026, 1, 1), created_at=NOW, updated_at=NOW,
        )
        session.add(mapping)
        session.flush()
    session.add(
        MonitoringObservation(
            asset_id=asset_id, provider_mapping_id=mapping.id, source_observation_key=key, source_revision=1,
            observed_at=observed_at, ingested_at=observed_at, condition=condition,
            freshness="unknown", quality="complete", completeness="complete", metadata_json={},
        )
    )


def test_an_installation_with_no_devices_is_still_evaluated(factory):
    """The whole gap in one test: 132 of 134 mapped plants own no imported device."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Só central")
        asset_id = asset.id
    with factory() as session, session.begin():
        observed_plant(session, asset_id=asset_id, condition="offline", observed_at=ago(minutes=5), key="k1")
    with factory() as session, session.begin():
        summary = evaluate_and_persist_incidents(session, now=NOW)
    assert summary.incidents_opened == 1
    with factory() as session:
        incident = session.scalar(select(DiagnosticIncident).where(DiagnosticIncident.asset_id == asset_id))
        assert incident is not None and incident.rule_code == "plant_offline" and incident.severity == "critical"
        assert incident.device_id is None


def test_a_plant_that_recovers_resolves_its_incident(factory):
    """The ✅ RESOLVIDO half: the next reading closes the episode by itself."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Cai e volta")
        asset_id = asset.id
    with factory() as session, session.begin():
        observed_plant(session, asset_id=asset_id, condition="offline", observed_at=ago(minutes=30), key="down")
    with factory() as session, session.begin():
        evaluate_and_persist_incidents(session, now=NOW)
    with factory() as session, session.begin():
        observed_plant(session, asset_id=asset_id, condition="operational", observed_at=ago(minutes=1), key="up")
    with factory() as session, session.begin():
        summary = evaluate_and_persist_incidents(session, now=NOW)
    assert summary.incidents_resolved == 1
    with factory() as session:
        incident = session.scalar(select(DiagnosticIncident).where(DiagnosticIncident.asset_id == asset_id))
        assert incident is not None and incident.status == "resolved" and incident.resolved_at is not None


def test_device_rules_still_run_for_assets_that_have_devices(factory):
    """Widening the population must not have narrowed what it already judged."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Com inversor")
        create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        asset_id = asset.id
    with factory() as session, session.begin():
        summary = evaluate_and_persist_incidents(session, now=NOW)
    assert summary.assets_evaluated == 1
    with factory() as session:
        codes = set(session.scalars(select(DiagnosticIncident.rule_code).where(DiagnosticIncident.asset_id == asset_id)))
        assert "device_no_history" in codes


# --- the schedule -------------------------------------------------------------


def test_the_plant_state_schedule_enqueues_once_per_interval(factory, settings):
    engine = create_engine(settings.database_url)
    repository = JobRepository(engine, factory)
    job, created = repository.enqueue_due_current_monitoring(connection_id=3, interval_minutes=15, now=NOW)
    assert created and job is not None and job.job_type == "monitoring.current"
    assert job.payload_json["connection_id"] == 3

    _again, created_again = repository.enqueue_due_current_monitoring(connection_id=3, interval_minutes=15, now=NOW + timedelta(minutes=5))
    assert not created_again, "a second tick inside the interval must not enqueue a second read"

    _next, created_next = repository.enqueue_due_current_monitoring(connection_id=3, interval_minutes=15, now=NOW + timedelta(minutes=16))
    assert created_next


def test_a_scheduler_stopped_for_days_reads_once_not_the_backlog(factory, settings):
    """Same catch-up guarantee as every other schedule (commit aac14ac)."""
    engine = create_engine(settings.database_url)
    repository = JobRepository(engine, factory)
    repository.enqueue_due_current_monitoring(connection_id=3, interval_minutes=15, now=NOW)
    created_count = 0
    for _ in range(5):
        _job, created = repository.enqueue_due_current_monitoring(connection_id=3, interval_minutes=15, now=NOW + timedelta(days=3))
        created_count += int(created)
    assert created_count == 1
