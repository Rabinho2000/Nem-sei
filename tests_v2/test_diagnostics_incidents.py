"""Diagnostic incidents (D1): persisted, deduplicated episodes of a finding.

Every test here proves a property from docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md's
D1 requirements directly, not just "the code runs": stable identity, open/resolved
with duration, first-detection/last-confirmation/resolution, dedup by episode
(not by every re-evaluation), provenance, and idempotency/restart-safety.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.findings import RULES_VERSION
from nemsei.diagnostics.incidents import evaluate_and_persist_incidents
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.diagnostics.service import record_device_status
from nemsei.monitoring.models import MonitoringObservation
from nemsei.providers.service import create_connection, create_mapping


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int = 12, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def asset_and_device(factory):
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Incident Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        ids = (asset.id, device.id)
    return factory, ids


def open_incidents(factory) -> list[DiagnosticIncident]:
    with factory() as session:
        return list(session.scalars(select(DiagnosticIncident).where(DiagnosticIncident.status == "open")))


def all_incidents(factory) -> list[DiagnosticIncident]:
    with factory() as session:
        return list(session.scalars(select(DiagnosticIncident)))


# --- identity, opening, provenance -------------------------------------------


def test_a_new_finding_opens_exactly_one_incident_with_stable_identity(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(), availability_status="unavailable",
        )
        summary = evaluate_and_persist_incidents(session, now=utc(13))

    assert summary.incidents_opened == 1
    assert summary.incidents_confirmed == 0
    assert summary.incidents_resolved == 0

    incidents = open_incidents(factory)
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.rule_code == "device_unavailable"
    assert incident.asset_id == asset_id
    assert incident.device_id == device_id
    assert incident.severity == "critical"
    assert incident.status == "open"
    assert incident.occurrence_count == 1
    assert incident.detector_version == RULES_VERSION
    assert incident.evidence_json.get("availability_status") == "unavailable"
    assert incident.resolved_at is None


def test_opened_at_uses_the_findings_own_active_since_not_evaluator_start_time(asset_and_device) -> None:
    """A problem that had already been going on for hours before the evaluator
    ever ran must not falsely claim to have started "now"."""
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="unavailable",
        )
        evaluate_and_persist_incidents(session, now=utc(13))

    incident = open_incidents(factory)[0]
    assert incident.opened_at == utc(9)
    assert incident.last_observed_at == utc(13)


# --- persistence across evaluations: dedup by episode -------------------------


def test_a_persistent_problem_stays_one_incident_across_many_evaluations(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="unavailable",
        )

    for hour in (13, 14, 15, 16):
        with factory() as session, session.begin():
            summary = evaluate_and_persist_incidents(session, now=utc(hour))
            if hour == 13:
                assert summary.incidents_opened == 1
                assert summary.incidents_confirmed == 0
            else:
                assert summary.incidents_opened == 0
                assert summary.incidents_confirmed == 1

    incidents = all_incidents(factory)
    assert len(incidents) == 1  # never a second row for the same ongoing problem
    incident = incidents[0]
    assert incident.status == "open"
    assert incident.opened_at == utc(9)  # unchanged across all four re-confirmations
    assert incident.last_observed_at == utc(16)  # advances to the latest confirmation
    assert incident.occurrence_count == 4


def test_running_the_evaluator_twice_in_a_row_is_idempotent_not_duplicating(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="unavailable",
        )
        evaluate_and_persist_incidents(session, now=utc(13))
    with factory() as session, session.begin():
        evaluate_and_persist_incidents(session, now=utc(13))  # same instant again

    assert len(all_incidents(factory)) == 1


def test_restarted_process_reconciles_from_persisted_state_alone(asset_and_device, settings) -> None:
    """A fresh session/engine (standing in for a worker restart) reconciles
    correctly using only what is in the database -- no in-memory state."""
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="unavailable",
        )
        evaluate_and_persist_incidents(session, now=utc(13))

    # A brand-new factory/engine, exactly as a restarted worker process would build.
    restarted_factory = build_session_factory(create_engine(settings.database_url))
    with restarted_factory() as session, session.begin():
        summary = evaluate_and_persist_incidents(session, now=utc(14))

    assert summary.incidents_opened == 0
    assert summary.incidents_confirmed == 1
    incident = open_incidents(factory)[0]
    assert incident.occurrence_count == 2
    assert incident.last_observed_at == utc(14)


# --- recovery: resolution, duration, and a fresh episode afterwards -----------


def test_recovery_resolves_the_incident_and_preserves_its_duration(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="unavailable",
        )
        evaluate_and_persist_incidents(session, now=utc(10))
    with factory() as session, session.begin():
        evaluate_and_persist_incidents(session, now=utc(13))  # one re-confirmation first

    with factory() as session, session.begin():
        # Recovery: a new reading under the same source_fact_key.
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(14), availability_status="available", active_power_kw=Decimal("6.0"),
        )
        summary = evaluate_and_persist_incidents(session, now=utc(14, 30))

    assert summary.incidents_resolved == 1
    assert summary.incidents_opened == 0

    incidents = all_incidents(factory)
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.status == "resolved"
    assert incident.opened_at == utc(9)
    assert incident.last_observed_at == utc(13)  # last time it was confirmed still broken
    assert incident.resolved_at is not None
    duration = incident.resolved_at - incident.opened_at
    assert duration >= timedelta(hours=5)  # 09:00 -> resolved at 14:30, real duration recoverable


def test_a_new_episode_after_resolution_is_a_new_incident_not_a_reused_row(asset_and_device) -> None:
    factory, (asset_id, device_id) = asset_and_device
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="unavailable",
        )
        evaluate_and_persist_incidents(session, now=utc(10))
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(11), availability_status="available",
        )
        evaluate_and_persist_incidents(session, now=utc(11, 30))  # resolves the first episode
    with factory() as session, session.begin():
        record_device_status(
            session, device_id=device_id, asset_id=asset_id, source_fact_key="v1:1",
            observed_at=utc(15), availability_status="unavailable",
        )
        evaluate_and_persist_incidents(session, now=utc(15, 30))  # a second, distinct episode

    incidents = sorted(all_incidents(factory), key=lambda row: row.id)
    assert len(incidents) == 2
    first, second = incidents
    assert first.status == "resolved"
    assert first.opened_at == utc(9)
    assert second.status == "open"
    assert second.opened_at == utc(15)
    assert second.id != first.id


# --- asset-level findings: nullable device_id, still deduplicated -------------


def test_an_asset_level_finding_opens_one_incident_with_a_null_device_id(factory) -> None:
    """`plant_offline`, not `partial_device_coverage`: both are asset-level
    (no `device_id`), but the coverage rule is `info` severity and, per the
    persistence policy below, never persists at all -- it cannot stand in for
    this proof any more. `plant_offline` is `critical`, bypasses the
    persistence gate, and is exactly as asset-level."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Offline Plant")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="main", display_name="FusionSolar",
            credential_reference="FUSIONSOLAR_MAIN", enabled=True, configuration_status="configured",
        )
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1")
        session.add(
            MonitoringObservation(
                asset_id=asset.id, provider_mapping_id=mapping.id, source_observation_key="k1",
                observed_at=utc(9), ingested_at=utc(9), condition="offline",
            )
        )
        session.flush()  # this factory's sessions are autoflush=False
        evaluate_and_persist_incidents(session, now=utc(10))
        evaluate_and_persist_incidents(session, now=utc(11))  # re-run: must not duplicate

    with factory() as session:
        offline_incidents = list(
            session.scalars(select(DiagnosticIncident).where(DiagnosticIncident.rule_code == "plant_offline"))
        )
    assert len(offline_incidents) == 1
    assert offline_incidents[0].device_id is None
    assert offline_incidents[0].occurrence_count == 2


# --- persistence policy: info never creates, warning waits ------------------


def test_an_info_severity_finding_never_becomes_an_incident(factory) -> None:
    """`partial_device_coverage` is the only `info`-severity rule today, and
    it only fires when devices are genuinely split between reporting and
    silent -- which is also exactly the condition that makes the silent
    device's own `device_no_history` (warning) fire alongside it. That
    second, unrelated incident is expected and asserted for; the point of
    this test is that the `info` one never joins it.
    GOAL.md's own policy: "Alarme informativo | não cria incidente"."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Partial Coverage Plant")
        reporting = create_device(session, asset_id=asset.id, device_kind="inverter", label="Reports", valid_from=date(2026, 1, 1))
        create_device(session, asset_id=asset.id, device_kind="inverter", label="Silent", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=reporting.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="available", active_power_kw=Decimal("5.0"),
        )
        first = evaluate_and_persist_incidents(session, now=utc(10))
        second = evaluate_and_persist_incidents(session, now=utc(11))  # re-run: still nothing, every time

    # `device_no_history` for "Silent" opens (then confirms) immediately --
    # it has no duration evidence to gate on at all. `partial_device_coverage`
    # is the one deferred, every single run, forever.
    assert first.incidents_opened == 1 and first.deferred == 1
    assert second.incidents_confirmed == 1 and second.incidents_opened == 0 and second.deferred == 1
    with factory() as session:
        rule_codes = {
            incident.rule_code for incident in session.scalars(select(DiagnosticIncident)).all()
        }
    assert rule_codes == {"device_no_history"}
    assert "partial_device_coverage" not in rule_codes


def test_the_database_itself_rejects_a_second_open_incident_for_the_same_null_device_identity(factory) -> None:
    """Not just application logic -- the partial unique index
    (COALESCE(device_id, -1)) must reject this even bypassing the evaluator."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Constraint Plant")
        asset_id = asset.id

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            session.add(
                DiagnosticIncident(
                    rule_code="partial_device_coverage", asset_id=asset_id, device_id=None,
                    severity="info", status="open", opened_at=utc(9), last_observed_at=utc(9),
                    occurrence_count=1, detector_version=RULES_VERSION, evidence_json={},
                    created_at=utc(9), updated_at=utc(9),
                )
            )
            session.add(
                DiagnosticIncident(
                    rule_code="partial_device_coverage", asset_id=asset_id, device_id=None,
                    severity="info", status="open", opened_at=utc(10), last_observed_at=utc(10),
                    occurrence_count=1, detector_version=RULES_VERSION, evidence_json={},
                    created_at=utc(10), updated_at=utc(10),
                )
            )


# --- multiple devices: independent incidents, no cross-contamination ----------


def test_independent_devices_get_independent_incidents(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Two Inverter Plant")
        down = create_device(session, asset_id=asset.id, device_kind="inverter", label="Down", valid_from=date(2026, 1, 1))
        healthy = create_device(session, asset_id=asset.id, device_kind="inverter", label="Healthy", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=down.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="unavailable",
        )
        record_device_status(
            session, device_id=healthy.id, asset_id=asset.id, source_fact_key="v1:2",
            observed_at=utc(9), availability_status="available", active_power_kw=Decimal("8.0"),
        )
        evaluate_and_persist_incidents(session, now=utc(10))

    incidents = open_incidents(factory)
    assert len(incidents) == 1
    assert incidents[0].device_id == down.id


# --- warning persistence gate --------------------------------------------


def test_a_new_warning_finding_is_deferred_below_the_threshold(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Unknown Status Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=utc(10, 0), availability_status="unknown",
        )
        summary = evaluate_and_persist_incidents(session, now=utc(10, 10))  # 10 min old

    assert summary.deferred == 1 and summary.incidents_opened == 0
    assert all_incidents(factory) == []


def test_a_warning_finding_creates_once_it_crosses_fifteen_minutes(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Unknown Status Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=utc(10, 0), availability_status="unknown",
        )
        evaluate_and_persist_incidents(session, now=utc(10, 10))  # still deferred
        summary = evaluate_and_persist_incidents(session, now=utc(10, 20))  # 20 min old: crosses 15

    assert summary.incidents_opened == 1 and summary.deferred == 0
    incidents = all_incidents(factory)
    assert len(incidents) == 1
    # opened_at is the finding's own evidenced start, not the moment the
    # evaluator finally acted on it.
    assert incidents[0].opened_at == utc(10, 0)


def test_a_critical_finding_creates_immediately_however_new(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Unavailable Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=utc(10, 0), availability_status="unavailable",
        )
        summary = evaluate_and_persist_incidents(session, now=utc(10, 1))  # one minute old

    assert summary.incidents_opened == 1


# --- recurrence -----------------------------------------------------------


def test_a_problem_recurring_three_times_in_24h_is_marked_recurring(factory) -> None:
    from nemsei.diagnostics.incidents import is_recurring

    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Flapping Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        asset_id, device_id = asset.id, device.id

    def episode(down_at: datetime, up_at: datetime) -> None:
        with factory() as session, session.begin():
            record_device_status(
                session, device_id=device_id, asset_id=asset_id, source_fact_key=f"down:{down_at.isoformat()}",
                observed_at=down_at, availability_status="unavailable",
            )
            evaluate_and_persist_incidents(session, now=down_at + timedelta(minutes=1))  # critical: immediate
        with factory() as session, session.begin():
            record_device_status(
                session, device_id=device_id, asset_id=asset_id, source_fact_key=f"up:{up_at.isoformat()}",
                observed_at=up_at, availability_status="available", active_power_kw=Decimal("4.0"),
            )
            evaluate_and_persist_incidents(session, now=up_at + timedelta(minutes=1))  # resolves it

    episode(utc(8, 0), utc(8, 30))
    episode(utc(12, 0), utc(12, 30))
    episode(utc(16, 0), utc(16, 30))

    with factory() as session:
        episodes = list(
            session.scalars(
                select(DiagnosticIncident)
                .where(DiagnosticIncident.rule_code == "device_unavailable")
                .order_by(DiagnosticIncident.opened_at)
            )
        )
        assert len(episodes) == 3
        assert is_recurring(session, incident=episodes[0]) is False
        assert is_recurring(session, incident=episodes[1]) is False
        assert is_recurring(session, incident=episodes[2]) is True


def test_recurrence_only_counts_within_the_rolling_window(factory) -> None:
    from nemsei.diagnostics.incidents import RECURRENCE_WINDOW, recurrence_count

    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Old Episodes Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        now = utc(12, 0)
        for hours_ago in (23, 30, 48):  # only the first is inside a 24h window
            incident = DiagnosticIncident(
                rule_code="device_unavailable", asset_id=asset.id, device_id=device.id, severity="critical", status="resolved",
                opened_at=now - timedelta(hours=hours_ago), last_observed_at=now - timedelta(hours=hours_ago),
                resolved_at=now - timedelta(hours=hours_ago) + timedelta(minutes=5), detector_version="test",
                created_at=now, updated_at=now,
            )
            session.add(incident)
        session.flush()
        count = recurrence_count(
            session, rule_code="device_unavailable", asset_id=asset.id, device_id=device.id, at=now, window=RECURRENCE_WINDOW
        )

    assert count == 1


# --- production window, end to end through the persisted evaluator ----------


def test_zero_production_in_daylight_creates_a_critical_incident_end_to_end(factory) -> None:
    """Proves the coordinate lookup and `window_for` wiring inside
    `evaluate_and_persist_incidents` itself, not just the pure rule."""
    from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset

    noon = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)  # Lisbon, deep summer noon: certainly daylight
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Lisbon Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        backfill_installations_from_assets(session)
        installation = installation_for_asset(session, asset_id=asset.id)
        installation.latitude = Decimal("38.7223")
        installation.longitude = Decimal("-9.1393")
        installation.coordinates_source = "manual"
        installation.coordinates_confidence = "manual"
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="k1",
            observed_at=noon - timedelta(minutes=40), availability_status="available", active_power_kw=Decimal("0"),
        )
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="k2",
            observed_at=noon, availability_status="available", active_power_kw=Decimal("0"),
        )
        summary = evaluate_and_persist_incidents(session, now=noon)

    assert summary.incidents_opened == 1
    incidents = all_incidents(factory)
    assert [incident.rule_code for incident in incidents] == ["zero_production_in_productive_window"]


def test_the_same_asset_at_night_does_not_get_the_production_incident(factory) -> None:
    from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset

    midnight = datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Lisbon Plant Night")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        backfill_installations_from_assets(session)
        installation = installation_for_asset(session, asset_id=asset.id)
        installation.latitude = Decimal("38.7223")
        installation.longitude = Decimal("-9.1393")
        installation.coordinates_source = "manual"
        installation.coordinates_confidence = "manual"
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="k1",
            observed_at=midnight - timedelta(minutes=40), availability_status="standby", active_power_kw=Decimal("0"),
        )
        record_device_status(
            session, device_id=device.id, asset_id=asset.id, source_fact_key="k2",
            observed_at=midnight, availability_status="standby", active_power_kw=Decimal("0"),
        )
        evaluate_and_persist_incidents(session, now=midnight)

    assert "zero_production_in_productive_window" not in {incident.rule_code for incident in all_incidents(factory)}
