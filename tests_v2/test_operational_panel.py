"""The home page answers "what needs attention now" instead of counting rows.

It used to report 219 360 production facts, 51 297 device states and 35 sync
runs. None of those is an operational answer, and the first is exactly the
number this codebase's rules say must never be read raw.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from nemsei.app import create_app
from nemsei.assets.service import create_asset, create_organization
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.web.panel import operational_panel
from nemsei.web.series import portfolio_monthly_series
from tests_v2.test_migrations import upgrade


def build(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    organization = create_organization(session, display_name="Sol PT")
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key="k", display_name="FS",
        credential_reference="ref", enabled=True, configuration_status="configured",
    )
    session.flush()
    return session, organization, connection


def add_asset(session, connection, *, name, power=None, mapped=True, external="NE=x"):
    asset = create_asset(session, canonical_name=name, timezone="Europe/Lisbon", installed_dc_power_kw=Decimal(str(power)) if power else None)
    session.flush()
    mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id=external) if mapped else None
    return asset, mapping


def add_fact(session, *, asset, mapping, day, value):
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    session.add(
        ProductionFact(
            asset_id=asset.id, provider_mapping_id=mapping.id,
            source_fact_key=f"p:{day.isoformat()}", source_revision=1,
            metric_kind="production_energy", period_start=start, period_end=start + timedelta(days=1),
            granularity="day", value=Decimal(str(value)), unit="kWh", quality="complete",
            completeness="complete", ingested_at=utc_now(), metadata_json={},
        )
    )


def client_for(settings):
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"] = True, "admin"
    return client


def test_an_installation_never_connected_outranks_one_that_is_merely_quiet(settings, monkeypatch) -> None:
    session, _, connection = build(settings, monkeypatch)
    quiet, quiet_mapping = add_asset(session, connection, name="Silenciosa", power=50, external="NE=1")
    add_fact(session, asset=quiet, mapping=quiet_mapping, day=utc_now().date() - timedelta(days=30), value="10")
    add_asset(session, connection, name="Nunca ligada", power=900, mapped=False)
    session.commit()

    panel = operational_panel(session)
    session.close()

    names = [row["asset"].canonical_name for row in panel["attention"]]
    assert names[0] == "Nunca ligada"
    assert panel["unmapped_assets"] == 1
    assert panel["silent_assets"] == 2


def test_a_critical_incident_outranks_everything_including_bigger_plants(settings, monkeypatch) -> None:
    session, _, connection = build(settings, monkeypatch)
    add_asset(session, connection, name="Grande sem provider", power=5000, mapped=False)
    broken, _ = add_asset(session, connection, name="Pequena parada", power=10, external="NE=2")
    session.flush()
    now = utc_now()
    session.add(
        DiagnosticIncident(
            rule_code="device_unavailable", asset_id=broken.id, severity="critical", status="open",
            opened_at=now, last_observed_at=now, occurrence_count=1, detector_version="1",
            evidence_json={}, created_at=now, updated_at=now,
        )
    )
    session.commit()

    panel = operational_panel(session)
    session.close()

    assert panel["attention"][0]["asset"].canonical_name == "Pequena parada"
    assert panel["critical"] == 1


def test_a_recently_reporting_installation_is_not_flagged(settings, monkeypatch) -> None:
    session, _, connection = build(settings, monkeypatch)
    healthy, mapping = add_asset(session, connection, name="Saudável", power=100, external="NE=3")
    add_fact(session, asset=healthy, mapping=mapping, day=utc_now().date(), value="55")
    session.commit()

    panel = operational_panel(session)
    session.close()

    assert panel["attention"] == []
    assert panel["reporting_recently"] == 1
    assert panel["silent_assets"] == 0


def test_the_portfolio_chart_reduces_superseded_revisions(settings, monkeypatch) -> None:
    session, _, connection = build(settings, monkeypatch)
    asset, mapping = add_asset(session, connection, name="Corrigida", external="NE=4")
    day = utc_now().date().replace(day=1)
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    for revision, value in ((1, "129280"), (2, "59560")):
        session.add(
            ProductionFact(
                asset_id=asset.id, provider_mapping_id=mapping.id, source_fact_key="p:same",
                source_revision=revision, metric_kind="production_energy",
                period_start=start, period_end=start + timedelta(days=1), granularity="day",
                value=Decimal(value), unit="kWh", quality="complete", completeness="complete",
                ingested_at=utc_now(), metadata_json={},
            )
        )
    session.commit()

    result = portfolio_monthly_series(session, total_assets=1)
    session.close()

    current_month = result["chart"].bars[-1]
    # 188.84 MWh would be the raw sum of both revisions.
    assert abs(current_month.point.value - 59.56) < 0.01
    assert current_month.point.coverage == 1.0


def test_the_panel_page_shows_triage_and_the_portfolio_chart(settings, monkeypatch) -> None:
    session, _, connection = build(settings, monkeypatch)
    add_asset(session, connection, name="Uma", mapped=False)
    session.commit()
    session.close()

    page = client_for(settings).get("/")

    assert page.status_code == 200
    assert "Precisa de atenção" in page.text
    assert "Sem provider" in page.text
    assert "Produção do portfolio" in page.text
    assert 'class="triage"' in page.text
    # The row counts it replaced must be gone from the headline.
    assert "factos de produção" not in page.text


def test_a_quiet_portfolio_looks_quiet(settings, monkeypatch) -> None:
    session, _, connection = build(settings, monkeypatch)
    healthy, mapping = add_asset(session, connection, name="Tudo bem", external="NE=5")
    add_fact(session, asset=healthy, mapping=mapping, day=utc_now().date(), value="40")
    session.commit()
    session.close()

    page = client_for(settings).get("/")

    assert "Nada precisa de atenção." in page.text
    # Normal state carries no colour: no severity stripe classes on the tiles.
    assert 'class="tri crit"' not in page.text
    assert 'class="tri warn"' not in page.text
