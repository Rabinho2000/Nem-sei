"""Portfolio Diagnostics (D5): aggregates DiagnosticIncident, never re-derives it.

Every test proves a requirement from the D5 approval directly: an asset in
two portfolios is counted correctly and independently in each; multiple
devices of one asset are one installation, not several; resolved incidents
never count as open; an incident-free portfolio reports zero cleanly, not
emptily; filters narrow correctly; installations sort worst-first; coverage
distinguishes complete/partial/none/no_devices (missing never becomes
healthy); and temporal membership (`on=`) is respected.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.portfolios.diagnostics import (
    RECENT_THRESHOLD_DAYS,
    portfolio_diagnostics_summary,
    portfolio_incident_rows,
    portfolio_installation_rows,
)
from nemsei.portfolios.service import add_member, create_portfolio
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


def make_incident(
    session, *, asset_id: int, device_id: int | None = None, rule_code: str = "device_unavailable",
    severity: str = "critical", status: str = "open", opened_at: datetime | None = None, resolved_at: datetime | None = None,
) -> DiagnosticIncident:
    opened = opened_at or utc(9)
    incident = DiagnosticIncident(
        rule_code=rule_code, asset_id=asset_id, device_id=device_id, severity=severity, status=status,
        opened_at=opened, last_observed_at=opened, resolved_at=resolved_at,
        occurrence_count=1, detector_version="1", evidence_json={}, created_at=opened, updated_at=opened,
    )
    session.add(incident)
    session.flush()
    return incident


def make_portfolio(session, name: str) -> int:
    return create_portfolio(session, name=name, created_by="tester").id


def add_current_member(session, *, portfolio_id: int, asset_id: int, valid_from: date = date(2026, 1, 1)) -> None:
    add_member(session, portfolio_id=portfolio_id, asset_id=asset_id, valid_from=valid_from, created_by="tester")


def mapping_for(session, asset_id: int, *, key: str, provider: str = "fusionsolar"):
    connection = session.scalar(text("SELECT id FROM provider_connections WHERE connection_key = :k").bindparams(k=key))
    if connection is None:
        connection = create_connection(
            session, provider_code=provider, connection_key=key, display_name=key,
            credential_reference="REF", enabled=True, configuration_status="configured",
        ).id
    return create_mapping(session, asset_id=asset_id, provider_connection_id=connection, external_id=f"EXT-{asset_id}")


# --- 1. an asset in two portfolios: correct and independent in each ----------


def test_an_asset_in_two_portfolios_is_counted_correctly_in_each_without_duplication(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Shared Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=asset.id, device_id=device.id, severity="critical")

        portfolio_a = make_portfolio(session, "Portfolio A")
        portfolio_b = make_portfolio(session, "Portfolio B")
        add_current_member(session, portfolio_id=portfolio_a, asset_id=asset.id)
        add_current_member(session, portfolio_id=portfolio_b, asset_id=asset.id)

        summary_a = portfolio_diagnostics_summary(session, portfolio_id=portfolio_a, on=date(2026, 7, 24))
        summary_b = portfolio_diagnostics_summary(session, portfolio_id=portfolio_b, on=date(2026, 7, 24))

    for summary in (summary_a, summary_b):
        assert summary.total_installations == 1
        assert summary.installations_with_incidents == 1  # not duplicated within either portfolio
        assert summary.incidents_critical == 1


# --- 2. multiple devices of one asset: one installation, correct incident count -


def test_multiple_devices_of_the_same_asset_count_as_one_installation(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Two Inverter Plant")
        device_a = create_device(session, asset_id=asset.id, device_kind="inverter", label="A", valid_from=date(2026, 1, 1))
        device_b = create_device(session, asset_id=asset.id, device_kind="inverter", label="B", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=asset.id, device_id=device_a.id, rule_code="device_unavailable", severity="critical")
        make_incident(session, asset_id=asset.id, device_id=device_b.id, rule_code="stale_reading", severity="warning")

        portfolio_id = make_portfolio(session, "Portfolio")
        add_current_member(session, portfolio_id=portfolio_id, asset_id=asset.id)

        summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))
        rows = portfolio_installation_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))

    assert summary.installations_with_incidents == 1  # one installation, not two
    assert summary.incidents_critical == 1
    assert summary.incidents_warning == 1
    assert summary.devices_affected == 2  # but both real devices are counted
    assert len(rows) == 1
    assert rows[0]["incident_count"] == 2


# --- 3. resolved incidents never count as open ---------------------------------


def test_resolved_incidents_do_not_count_as_open(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Recovered Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=asset.id, device_id=device.id, status="resolved", opened_at=utc(9), resolved_at=utc(11))

        portfolio_id = make_portfolio(session, "Portfolio")
        add_current_member(session, portfolio_id=portfolio_id, asset_id=asset.id)

        summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))
        open_rows = portfolio_incident_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24), filters={"status": "open"})
        resolved_rows = portfolio_incident_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24), filters={"status": "resolved"})

    assert summary.installations_with_incidents == 0
    assert summary.incidents_critical == 0
    assert open_rows == []
    assert len(resolved_rows) == 1


# --- 4. a portfolio with no incidents reports zero cleanly ---------------------


def test_a_portfolio_with_no_incidents_reports_zero_cleanly(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Quiet Plant")
        create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        portfolio_id = make_portfolio(session, "Portfolio")
        add_current_member(session, portfolio_id=portfolio_id, asset_id=asset.id)

        summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))

    assert summary.total_installations == 1
    assert summary.installations_with_incidents == 0
    assert summary.installations_healthy == 1
    assert summary.incidents_critical == 0
    assert summary.incidents_warning == 0
    assert summary.incidents_info == 0
    # "Healthy" here trusts the persisted incident state exactly as D1 left
    # it (this module never re-derives a finding) -- see the dedicated
    # coverage test below for the complete/partial/none/no_devices distinction
    # that keeps a genuinely data-less installation from reading as healthy.


# --- 7. coverage: complete / partial / none / no_devices, missing != healthy --


def test_coverage_distinguishes_complete_partial_none_and_no_devices(factory) -> None:
    with factory() as session, session.begin():
        # Complete: a device with history and no incident.
        complete_asset = create_asset(session, canonical_name="Complete Plant")
        create_device(session, asset_id=complete_asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))

        # Partial: one of two devices has a device_no_history incident.
        partial_asset = create_asset(session, canonical_name="Partial Plant")
        partial_device_ok = create_device(session, asset_id=partial_asset.id, device_kind="inverter", label="OK", valid_from=date(2026, 1, 1))
        partial_device_missing = create_device(session, asset_id=partial_asset.id, device_kind="inverter", label="Missing", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=partial_asset.id, device_id=partial_device_missing.id, rule_code="device_no_history", severity="warning")

        # None: the only device has a device_no_history incident.
        none_asset = create_asset(session, canonical_name="No Data Plant")
        none_device = create_device(session, asset_id=none_asset.id, device_kind="inverter", label="Missing", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=none_asset.id, device_id=none_device.id, rule_code="device_no_history", severity="warning")

        # No devices at all -- must never read as "healthy".
        empty_asset = create_asset(session, canonical_name="No Devices Plant")

        portfolio_id = make_portfolio(session, "Portfolio")
        for asset in (complete_asset, partial_asset, none_asset, empty_asset):
            add_current_member(session, portfolio_id=portfolio_id, asset_id=asset.id)

        rows = {row["asset_id"]: row for row in portfolio_installation_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))}
        summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))

    assert rows[complete_asset.id]["coverage"] == "complete"
    assert rows[partial_asset.id]["coverage"] == "partial"
    assert rows[none_asset.id]["coverage"] == "none"
    assert rows[empty_asset.id]["coverage"] == "no_devices"

    assert summary.installations_no_devices == 1
    assert summary.installations_full_coverage == 1  # only the truly complete one
    # "Missing never becomes healthy": exactly the genuinely complete asset
    # counts as healthy -- the no-devices asset has zero incidents too (there
    # is nothing to evaluate), but must never be counted alongside it.
    assert summary.installations_healthy == 1


# --- 6. installations sort worst-first ------------------------------------------


def test_installation_rows_sort_worst_first(factory) -> None:
    with factory() as session, session.begin():
        healthy = create_asset(session, canonical_name="Alpha Healthy")
        create_device(session, asset_id=healthy.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))

        warning_asset = create_asset(session, canonical_name="Beta Warning")
        warning_device = create_device(session, asset_id=warning_asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=warning_asset.id, device_id=warning_device.id, rule_code="stale_reading", severity="warning")

        critical_asset = create_asset(session, canonical_name="Zulu Critical")
        critical_device = create_device(session, asset_id=critical_asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=critical_asset.id, device_id=critical_device.id, rule_code="device_unavailable", severity="critical")

        portfolio_id = make_portfolio(session, "Portfolio")
        for asset in (healthy, warning_asset, critical_asset):
            add_current_member(session, portfolio_id=portfolio_id, asset_id=asset.id)

        rows = portfolio_installation_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))

    assert [row["name"] for row in rows] == ["Zulu Critical", "Beta Warning", "Alpha Healthy"]


# --- 5. filters ------------------------------------------------------------------


def test_filters_narrow_the_incident_list(factory) -> None:
    with factory() as session, session.begin():
        asset_1 = create_asset(session, canonical_name="Asset One")
        device_1 = create_device(session, asset_id=asset_1.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=asset_1.id, device_id=device_1.id, rule_code="device_unavailable", severity="critical")
        mapping_for(session, asset_1.id, key="conn-1", provider="fusionsolar")

        asset_2 = create_asset(session, canonical_name="Asset Two")
        device_2 = create_device(session, asset_id=asset_2.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=asset_2.id, device_id=device_2.id, rule_code="stale_reading", severity="warning")
        mapping_for(session, asset_2.id, key="conn-2", provider="sigenergy")

        portfolio_id = make_portfolio(session, "Portfolio")
        add_current_member(session, portfolio_id=portfolio_id, asset_id=asset_1.id)
        add_current_member(session, portfolio_id=portfolio_id, asset_id=asset_2.id)

        by_severity = portfolio_incident_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24), filters={"severity": "critical"})
        by_rule = portfolio_incident_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24), filters={"rule": "stale_reading"})
        by_asset = portfolio_incident_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24), filters={"asset_id": str(asset_1.id)})
        by_provider = portfolio_incident_rows(session, portfolio_id=portfolio_id, on=date(2026, 7, 24), filters={"provider_code": "sigenergy"})

    assert len(by_severity) == 1 and by_severity[0]["incident"].rule_code == "device_unavailable"
    assert len(by_rule) == 1 and by_rule[0]["incident"].rule_code == "stale_reading"
    assert len(by_asset) == 1 and by_asset[0]["asset_id"] == asset_1.id
    assert len(by_provider) == 1 and by_provider[0]["asset_id"] == asset_2.id


# --- 8. historical membership is respected --------------------------------------


def test_historical_membership_is_respected(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Left The Portfolio")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        make_incident(session, asset_id=asset.id, device_id=device.id, severity="critical")

        portfolio_id = make_portfolio(session, "Portfolio")
        # A member from January through March only.
        add_member(
            session, portfolio_id=portfolio_id, asset_id=asset.id, valid_from=date(2026, 1, 1),
            valid_to=date(2026, 3, 31), created_by="tester",
        )

        # As of today (July): no longer a member, so no incident counted.
        today_summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio_id, on=date(2026, 7, 24))
        # As of a date within the membership window: correctly counted.
        past_summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio_id, on=date(2026, 2, 15))

    assert today_summary.total_installations == 0
    assert today_summary.installations_with_incidents == 0
    assert past_summary.total_installations == 1
    assert past_summary.installations_with_incidents == 1


# --- recent vs. historical backlog ------------------------------------------------


def test_recent_and_historical_backlog_are_distinguished(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Mixed Age Plant")
        device_old = create_device(session, asset_id=asset.id, device_kind="inverter", label="Old", valid_from=date(2026, 1, 1))
        device_new = create_device(session, asset_id=asset.id, device_kind="inverter", label="New", valid_from=date(2026, 1, 1))
        now = datetime.now(timezone.utc)
        make_incident(session, asset_id=asset.id, device_id=device_old.id, rule_code="stale_reading", severity="warning", opened_at=now - timedelta(days=95))
        make_incident(session, asset_id=asset.id, device_id=device_new.id, rule_code="device_unavailable", severity="critical", opened_at=now - timedelta(hours=2))

        portfolio_id = make_portfolio(session, "Portfolio")
        add_current_member(session, portfolio_id=portfolio_id, asset_id=asset.id)

        summary = portfolio_diagnostics_summary(session, portfolio_id=portfolio_id, on=date.today(), now=now)
        rows = portfolio_installation_rows(session, portfolio_id=portfolio_id, on=date.today(), now=now)

    assert summary.recent_incidents == 1
    assert summary.historical_backlog_incidents == 1
    assert summary.oldest_incident_age_days == pytest.approx(95, abs=0.1)
    assert rows[0]["has_recent_incident"] is True
    assert RECENT_THRESHOLD_DAYS == 7  # documented, not silently changed
