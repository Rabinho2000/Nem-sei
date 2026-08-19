"""The monthly workflow: validate coverage, generate, review, approve.

Every assertion here checks either a state transition rule or that the run's
numbers actually came from the individual reports it points at — never a
recalculation of its own.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.portfolios.reporting import (
    approve_run,
    existing_run,
    generate_report_run,
    mark_run_reviewed,
    run_member_rows,
    validate_coverage,
)
from nemsei.portfolios.service import add_member, create_portfolio
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.models import ReportSnapshot


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def add_production(session, asset_id, mapping_id, day: date, value):
    return record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"production_energy:{asset_id}:{day.isoformat()}",
        period_start=utc(day),
        period_end=utc(date.fromordinal(day.toordinal() + 1)),
        granularity="day",
        metric_kind="production_energy",
        value=Decimal(str(value)),
        unit="kWh",
        quality="complete",
        completeness="complete",
        metadata={},
    )


def mapping_for(session, asset_id, key):
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key=key, display_name=key,
        credential_reference="REF", enabled=True, configuration_status="configured",
    )
    return create_mapping(session, asset_id=asset_id, provider_connection_id=connection.id, external_id=f"EXT-{asset_id}")


@pytest.fixture
def portfolio_with_members(factory):
    """One asset that will report, one that never will."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        reporting = create_asset(session, canonical_name="Reports Fine")
        mapping = mapping_for(session, reporting.id, "c-reports")
        add_production(session, reporting.id, mapping.id, date(2026, 3, 10), "100")
        silent = create_asset(session, canonical_name="Never Reports")
        for asset in (reporting, silent):
            add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        ids = (portfolio.id, reporting.id, silent.id)
    return factory, ids


# --- validating coverage and generating -------------------------------------


def test_validating_coverage_creates_no_run(portfolio_with_members) -> None:
    factory, (portfolio_id, *_rest) = portfolio_with_members
    with factory() as session, session.begin():
        validate_coverage(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
    with factory() as session:
        assert existing_run(session, portfolio_id=portfolio_id, period_start=date(2026, 3, 1), period_end=date(2026, 4, 1)) is None


def test_generating_reports_the_ready_and_blocks_the_silent_with_a_reason(portfolio_with_members) -> None:
    factory, (portfolio_id, reporting_id, silent_id) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="ines")
        assert run.status == "generated"
        assert run.generated_by == "ines"
        rows = run_member_rows(session, run)

    by_asset = {row["asset_id"]: row for row in rows}
    assert by_asset[reporting_id]["status"] == "ready"
    assert by_asset[reporting_id]["report_snapshot_id"] is not None
    assert by_asset[silent_id]["status"] == "blocked"
    assert by_asset[silent_id]["reason"] == "sem_dados_de_producao_no_periodo"


def test_the_individual_snapshot_matches_what_an_individual_report_would_say(portfolio_with_members) -> None:
    """If this number ever disagreed with the asset's own report, that would
    be a bug in the workflow, not a difference of method.
    """
    factory, (portfolio_id, reporting_id, _silent_id) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
        member = next(m for m in run.members if m.asset_id == reporting_id)
        snapshot = session.get(ReportSnapshot, member.report_snapshot_id)
        assert snapshot.payload_json["production_kwh"] == 100.0


def test_regenerating_before_approval_replaces_members_and_resets_review(portfolio_with_members) -> None:
    factory, (portfolio_id, reporting_id, silent_id) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
        run_id = run.id
        mark_run_reviewed(session, run_id=run_id, actor="op", notes="looks fine")

    # The silent asset gets a fact between the review and the regeneration.
    with factory() as session, session.begin():
        mapping = mapping_for(session, silent_id, "c-silent")
        add_production(session, silent_id, mapping.id, date(2026, 3, 12), "40")
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op2")

    assert run.id == run_id
    assert run.status == "generated"
    assert run.reviewed_at is None and run.reviewed_by is None
    with factory() as session:
        rows = run_member_rows(session, session.get(run.__class__, run_id))
        by_asset = {row["asset_id"]: row["status"] for row in rows}
    # The silent asset now has a fact, so regeneration finds it ready.
    assert by_asset[silent_id] == "ready"
    assert by_asset[reporting_id] == "ready"


# --- the state machine -------------------------------------------------------


def test_a_generated_run_cannot_be_approved_directly(portfolio_with_members) -> None:
    factory, (portfolio_id, *_rest) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
        run_id = run.id
    with pytest.raises(ValueError, match="reviewed run can be approved"):
        with factory() as session, session.begin():
            approve_run(session, run_id=run_id, actor="op")


def test_a_reviewed_run_cannot_be_reviewed_again(portfolio_with_members) -> None:
    factory, (portfolio_id, *_rest) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
        run_id = run.id
        mark_run_reviewed(session, run_id=run_id, actor="op")
    with pytest.raises(ValueError, match="generated run can be marked reviewed"):
        with factory() as session, session.begin():
            mark_run_reviewed(session, run_id=run_id, actor="op")


def test_the_full_state_machine_end_to_end(portfolio_with_members) -> None:
    factory, (portfolio_id, *_rest) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
        run_id = run.id
    with factory() as session, session.begin():
        mark_run_reviewed(session, run_id=run_id, actor="reviewer", notes="ok")
    with factory() as session, session.begin():
        approved = approve_run(session, run_id=run_id, actor="approver")
        assert approved.status == "approved"
        assert approved.approved_by == "approver"


def test_an_approved_run_cannot_be_regenerated(portfolio_with_members) -> None:
    factory, (portfolio_id, *_rest) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
        run_id = run.id
        mark_run_reviewed(session, run_id=run_id, actor="op")
        approve_run(session, run_id=run_id, actor="op")
    with pytest.raises(ValueError, match="cannot be regenerated"):
        with factory() as session, session.begin():
            generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")


def test_an_approved_run_is_locked_at_the_database_too(portfolio_with_members) -> None:
    """The trigger, not just the service layer: the database itself refuses."""
    factory, (portfolio_id, *_rest) = portfolio_with_members
    with factory() as session, session.begin():
        run = generate_report_run(session, portfolio_id=portfolio_id, report_month="2026-03", actor="op")
        run_id = run.id
        mark_run_reviewed(session, run_id=run_id, actor="op")
        approve_run(session, run_id=run_id, actor="op")

    with pytest.raises(Exception, match="cannot be changed"):
        with factory() as session, session.begin():
            session.execute(
                text("UPDATE portfolio_report_runs SET review_notes = 'tampered' WHERE id = :r"), {"r": run_id}
            )

    with pytest.raises(Exception, match="cannot be changed"):
        with factory() as session, session.begin():
            member_id = session.scalar(
                text("SELECT id FROM portfolio_report_run_members WHERE run_id = :r LIMIT 1"), {"r": run_id}
            )
            session.execute(
                text("UPDATE portfolio_report_run_members SET reason = 'tampered' WHERE id = :m"), {"m": member_id}
            )

    with pytest.raises(Exception, match="cannot be deleted"):
        with factory() as session, session.begin():
            session.execute(text("DELETE FROM portfolio_report_runs WHERE id = :r"), {"r": run_id})
