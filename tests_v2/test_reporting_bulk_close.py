"""Generate everything a month has left, then close it.

`generate_all_reports` is a bulk pass over the exact `assemble_asset_report`/
`snapshot_dataset`/`generate_report_run` calls every other generate button
already makes -- these tests pin that it reaches every reportable asset and
every un-run portfolio, skips what has no energy, and is safe to repeat.
`close_month` is the new enforcement point: it refuses while an ESCO still
lacks billing configuration or a tariff, unless the caller explicitly
overrides, and otherwise approves every run the month owes.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.portfolios.reporting import approve_run, existing_run, mark_run_reviewed
from nemsei.portfolios.service import add_member, create_portfolio
from nemsei.providers.models import OperatorAuditEvent
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.bulk_close import close_month, generate_all_reports, month_close_blockers
from nemsei.reporting.commercial import set_billing_config, set_tariff
from nemsei.reporting.models import ReportingDataset, ReportSnapshot


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


def add_production(session, asset_id, mapping_id, day: date, value, metric="production_energy"):
    return record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"{metric}:{asset_id}:{day.isoformat()}",
        period_start=utc(day),
        period_end=utc(date.fromordinal(day.toordinal() + 1)),
        granularity="day",
        metric_kind=metric,
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


def count_snapshots(factory, asset_id: int) -> int:
    with factory() as session:
        return len(
            session.scalars(
                select(ReportSnapshot.id)
                .join(ReportingDataset, ReportingDataset.id == ReportSnapshot.dataset_id)
                .where(ReportingDataset.asset_id == asset_id)
            ).all()
        )


@pytest.fixture
def world(factory):
    """A portfolio with one reporting member, a standalone reporting asset
    outside any portfolio, an asset with no energy at all, and an ESCO asset
    with production but neither billing configuration nor a tariff."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        member = create_asset(session, canonical_name="Portfolio Member")
        mapping = mapping_for(session, member.id, "c-member")
        add_production(session, member.id, mapping.id, date(2026, 3, 10), "100")
        add_member(session, portfolio_id=portfolio.id, asset_id=member.id, valid_from=date(2026, 1, 1), created_by="op")

        standalone = create_asset(session, canonical_name="Standalone")
        standalone_mapping = mapping_for(session, standalone.id, "c-standalone")
        add_production(session, standalone.id, standalone_mapping.id, date(2026, 3, 10), "50")

        silent = create_asset(session, canonical_name="No Energy")

        esco = create_asset(session, canonical_name="Esco Unpriced")
        esco.contract_type = "ESCO"
        esco_mapping = mapping_for(session, esco.id, "c-esco")
        add_production(session, esco.id, esco_mapping.id, date(2026, 3, 10), "80")

        ids = {
            "portfolio": portfolio.id,
            "member": member.id,
            "standalone": standalone.id,
            "silent": silent.id,
            "esco": esco.id,
        }
    return factory, ids


# --- generate_all_reports ----------------------------------------------------


def test_generate_all_reports_reaches_every_reportable_asset_and_portfolio(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        result = generate_all_reports(session, month="2026-03", actor="op")

    assert result.generated_assets == 3  # member, standalone, esco
    assert result.skipped_no_energy == 1  # silent
    assert result.generated_portfolio_runs == 1
    assert result.already_had_run == 0
    assert count_snapshots(factory, ids["member"]) == 1
    assert count_snapshots(factory, ids["standalone"]) == 1
    assert count_snapshots(factory, ids["silent"]) == 0
    with factory() as session:
        assert existing_run(session, portfolio_id=ids["portfolio"], period_start=date(2026, 3, 1), period_end=date(2026, 4, 1)) is not None


def test_generate_all_reports_reports_who_it_is_still_waiting_on(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        result = generate_all_reports(session, month="2026-03", actor="op")
    assert ids["esco"] in {row.asset_id for row in result.blocking}


def test_generate_all_reports_is_safe_to_repeat(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        generate_all_reports(session, month="2026-03", actor="op")
    with factory() as session, session.begin():
        second = generate_all_reports(session, month="2026-03", actor="op2")

    assert second.generated_portfolio_runs == 0
    assert second.already_had_run == 1
    # `assemble_asset_report`/`snapshot_dataset` are themselves idempotent
    # (a new snapshot only when something actually changed), so a repeat
    # does not pile up duplicate snapshots for the same unchanged month.
    assert count_snapshots(factory, ids["standalone"]) == 1


def test_generate_all_reports_never_touches_an_approved_run(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        generate_all_reports(session, month="2026-03", actor="op")
        run = existing_run(session, portfolio_id=ids["portfolio"], period_start=date(2026, 3, 1), period_end=date(2026, 4, 1))
        run_id = run.id
        mark_run_reviewed(session, run_id=run_id, actor="op")
        approve_run(session, run_id=run_id, actor="op")

    with factory() as session, session.begin():
        generate_all_reports(session, month="2026-03", actor="op2")

    with factory() as session:
        run = existing_run(session, portfolio_id=ids["portfolio"], period_start=date(2026, 3, 1), period_end=date(2026, 4, 1))
        assert run.status == "approved"


# --- close_month --------------------------------------------------------


def test_close_month_refuses_while_an_esco_lacks_billing_config(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        generate_all_reports(session, month="2026-03", actor="op")
    with pytest.raises(ValueError, match="Esco Unpriced"):
        with factory() as session, session.begin():
            close_month(session, month="2026-03", actor="op")


def test_close_month_refuses_while_an_esco_has_billing_but_no_tariff(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        set_billing_config(
            session, asset_id=ids["esco"], report_type="esco", valid_from=date(2026, 1, 1),
            created_by="op", solcor_price_per_kwh=Decimal("0.1"),
            default_electricity_price=Decimal("0.06"), default_export_price=Decimal("0.04"),
        )
        generate_all_reports(session, month="2026-03", actor="op")
    with factory() as session:
        blockers = month_close_blockers(session, month="2026-03")
    assert any(row.asset_id == ids["esco"] and not row.has_tariff for row in blockers)
    with pytest.raises(ValueError, match="Esco Unpriced"):
        with factory() as session, session.begin():
            close_month(session, month="2026-03", actor="op")


def test_close_month_approves_everything_once_the_esco_is_fully_priced(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        set_billing_config(
            session, asset_id=ids["esco"], report_type="esco", valid_from=date(2026, 1, 1),
            created_by="op", solcor_price_per_kwh=Decimal("0.1"),
            default_electricity_price=Decimal("0.06"), default_export_price=Decimal("0.04"),
        )
        set_tariff(
            session, asset_id=ids["esco"], tariff_type="simple", valid_from=date(2026, 1, 1),
            created_by="op", prices={"simple": Decimal("0.2")},
        )
        generate_all_reports(session, month="2026-03", actor="op")
    with factory() as session:
        assert month_close_blockers(session, month="2026-03") == ()

    with factory() as session, session.begin():
        approved = close_month(session, month="2026-03", actor="op")
    assert len(approved) == 1
    assert approved[0].status == "approved"


def test_close_month_carries_a_generated_run_through_review_on_the_way_to_approval(world) -> None:
    """`approve_run` only accepts a `reviewed` run; closing the month must
    not crash on one that never got there yet."""
    factory, ids = world
    with factory() as session, session.begin():
        set_billing_config(
            session, asset_id=ids["esco"], report_type="esco", valid_from=date(2026, 1, 1),
            created_by="op", solcor_price_per_kwh=Decimal("0.1"),
            default_electricity_price=Decimal("0.06"), default_export_price=Decimal("0.04"),
        )
        set_tariff(
            session, asset_id=ids["esco"], tariff_type="simple", valid_from=date(2026, 1, 1),
            created_by="op", prices={"simple": Decimal("0.2")},
        )
        result = generate_all_reports(session, month="2026-03", actor="op")
        assert result.generated_portfolio_runs == 1
    with factory() as session:
        run = existing_run(session, portfolio_id=ids["portfolio"], period_start=date(2026, 3, 1), period_end=date(2026, 4, 1))
        assert run.status == "generated"

    with factory() as session, session.begin():
        close_month(session, month="2026-03", actor="op")

    with factory() as session:
        run = existing_run(session, portfolio_id=ids["portfolio"], period_start=date(2026, 3, 1), period_end=date(2026, 4, 1))
        assert run.status == "approved"


def test_close_month_override_proceeds_despite_gaps_and_is_audited(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        generate_all_reports(session, month="2026-03", actor="op")

    with factory() as session, session.begin():
        approved = close_month(session, month="2026-03", actor="ines", override_reason="Central offline, aprovado pelo cliente por email.")
    assert len(approved) == 1
    assert approved[0].status == "approved"

    with factory() as session:
        event = session.scalar(select(OperatorAuditEvent).where(OperatorAuditEvent.action == "month_closed"))
    assert event is not None
    assert event.actor_username == "ines"
    assert event.metadata_json["decision"] == "closed_with_gaps"
    assert event.metadata_json["asset_count"] == 1
    # The free-text reason is never persisted in this audit trail.
    assert "override_reason" not in event.metadata_json
    assert "email" not in str(event.metadata_json)


def test_close_month_does_not_touch_an_already_approved_run_on_a_second_call(world) -> None:
    factory, ids = world
    with factory() as session, session.begin():
        set_billing_config(
            session, asset_id=ids["esco"], report_type="esco", valid_from=date(2026, 1, 1),
            created_by="op", solcor_price_per_kwh=Decimal("0.1"),
            default_electricity_price=Decimal("0.06"), default_export_price=Decimal("0.04"),
        )
        set_tariff(
            session, asset_id=ids["esco"], tariff_type="simple", valid_from=date(2026, 1, 1),
            created_by="op", prices={"simple": Decimal("0.2")},
        )
        generate_all_reports(session, month="2026-03", actor="op")
    with factory() as session, session.begin():
        close_month(session, month="2026-03", actor="op")
    # Second call: nothing left to approve, and it must not raise trying to
    # re-approve an already-approved run.
    with factory() as session, session.begin():
        second = close_month(session, month="2026-03", actor="op2")
    assert second == []
