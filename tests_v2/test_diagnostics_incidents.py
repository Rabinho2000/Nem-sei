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
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Partial Coverage Plant")
        reporting = create_device(session, asset_id=asset.id, device_kind="inverter", label="Reports", valid_from=date(2026, 1, 1))
        create_device(session, asset_id=asset.id, device_kind="inverter", label="Silent", valid_from=date(2026, 1, 1))
        record_device_status(
            session, device_id=reporting.id, asset_id=asset.id, source_fact_key="v1:1",
            observed_at=utc(9), availability_status="available", active_power_kw=Decimal("5.0"),
        )
        evaluate_and_persist_incidents(session, now=utc(10))
        evaluate_and_persist_incidents(session, now=utc(11))  # re-run: must not duplicate

    with factory() as session:
        coverage_incidents = list(
            session.scalars(select(DiagnosticIncident).where(DiagnosticIncident.rule_code == "partial_device_coverage"))
        )
    assert len(coverage_incidents) == 1
    assert coverage_incidents[0].device_id is None
    assert coverage_incidents[0].occurrence_count == 2


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
