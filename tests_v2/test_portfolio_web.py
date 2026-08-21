"""The portfolio screens render, filter, and never leak a wrong total."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.app import create_app
from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.monitoring.service import record_production_fact
from nemsei.portfolios.datasets import build_portfolio_dataset
from nemsei.portfolios.service import add_member, create_portfolio, freeze_snapshot
from nemsei.providers.service import create_connection, create_mapping
from nemsei.web.portfolio_queries import SECTIONS


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    return create_app(settings)


@pytest.fixture
def seeded(app, settings):
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="Solcorelios I", created_by="op")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        reporting = create_asset(
            session, canonical_name="Reporting Plant", country_code="PT",
            installed_dc_power_kw=Decimal("120.5"),
        )
        mapping = create_mapping(
            session, asset_id=reporting.id, provider_connection_id=connection.id, external_id="NE=1"
        )
        record_production_fact(
            session, asset_id=reporting.id, provider_mapping_id=mapping.id,
            source_fact_key="p:1", period_start=datetime.combine(date(2026, 3, 10), time.min, tzinfo=timezone.utc),
            period_end=datetime.combine(date(2026, 3, 11), time.min, tzinfo=timezone.utc),
            granularity="day", value=Decimal("1000"), unit="kWh",
            quality="complete", completeness="complete", metadata={},
        )
        silent = create_asset(session, canonical_name="Silent Plant", country_code="ES")
        for asset in (reporting, silent):
            add_member(
                session, portfolio_id=portfolio.id, asset_id=asset.id,
                valid_from=date(2026, 1, 1), created_by="op",
            )
        add_member(
            session, portfolio_id=portfolio.id, valid_from=date(2026, 1, 1), created_by="op",
            sub_account="040", external_name="Consumidor Final", tax_id="999999990",
        )
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        build_portfolio_dataset(session, snapshot=snapshot, built_by="op")
        portfolio_id = portfolio.id
    return portfolio_id


def login(client) -> None:
    """Set the session directly, as the other web tests do."""
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True
        browser_session["username"] = "admin"


def test_every_section_renders(app, seeded) -> None:
    client = app.test_client()
    login(client)
    for key, label in SECTIONS:
        response = client.get(f"/portfolios/{seeded}/{key}?month=2026-03")
        assert response.status_code == 200, f"{key} did not render"
        assert "Solcorelios I" in response.get_data(as_text=True)


def test_an_unknown_section_is_not_invented(app, seeded) -> None:
    client = app.test_client()
    login(client)
    assert client.get(f"/portfolios/{seeded}/nonsense").status_code == 404


def test_the_screens_require_authentication(app, seeded) -> None:
    response = app.test_client().get(f"/portfolios/{seeded}")
    assert response.status_code in (302, 401)


def test_the_overview_names_the_installation_needing_attention(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/overview?month=2026-03").get_data(as_text=True)
    assert "Silent Plant" in body
    assert "Precisam de atenção" in body
    # The coverage the operator asks for, stated plainly.
    assert "1/2" in body


def test_a_partial_total_is_labelled_partial_rather_than_shown_as_complete(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/production?month=2026-03").get_data(as_text=True)
    assert "1 000" in body or "1,000" in body or "1000" in body
    assert "parcial" in body


def test_country_is_a_filter_and_not_a_sub_portfolio(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/installations?month=2026-03&country_code=PT").get_data(as_text=True)
    assert "Reporting Plant" in body
    assert "Silent Plant" not in body
    # Filtering never creates another portfolio.
    assert client.get("/portfolios").get_data(as_text=True).count("Solcorelios I") == 1


def test_the_attention_filter_narrows_to_what_is_wrong(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/installations?month=2026-03&attention=1").get_data(as_text=True)
    assert "Silent Plant" in body
    assert "Reporting Plant" not in body


def test_an_unresolved_member_is_shown_as_unresolved_not_as_a_plant(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/installations?month=2026-03").get_data(as_text=True)
    assert "Consumidor Final" in body
    assert "por resolver" in body


def test_a_period_without_a_dataset_says_so_instead_of_showing_zeros(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/overview?month=2026-09").get_data(as_text=True)
    assert "Sem dados construídos" in body
    assert "Construir" in body


def csrf_token(response) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def test_the_review_screen_offers_a_nif_match_as_evidence_not_a_decision(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/members/review").get_data(as_text=True)
    assert "Consumidor Final" in body
    assert "Nada aqui associa sozinho" in body


def test_resolving_a_member_from_the_review_screen_returns_there(app, seeded, settings) -> None:
    client = app.test_client()
    login(client)
    from sqlalchemy import create_engine, select

    from nemsei.assets.service import create_asset
    from nemsei.db.session import build_session_factory
    from nemsei.portfolios.models import PortfolioMembership

    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        # An installation that is not already a member of this portfolio.
        asset_id = create_asset(session, canonical_name="Newly Discovered Plant").id
    with factory() as session:
        membership_id = session.scalar(
            select(PortfolioMembership.id).where(PortfolioMembership.sub_account == "040")
        )

    form = client.get(f"/portfolios/{seeded}/members/review")
    response = client.post(
        f"/portfolios/{seeded}/members/{membership_id}/resolve",
        data={"csrf_token": csrf_token(form), "asset_id": str(asset_id), "next": "review"},
    )
    assert response.status_code == 302
    assert response.location.endswith(f"/portfolios/{seeded}/members/review")

    with factory() as session:
        membership = session.get(PortfolioMembership, membership_id)
        assert membership.resolution_state == "resolved"
        assert membership.asset_id == asset_id


def test_resolving_a_member_to_an_asset_already_in_the_portfolio_is_rejected_cleanly(app, seeded, settings) -> None:
    """A raw IntegrityError reaching the browser is a 500; a friendly error is not."""
    client = app.test_client()
    login(client)
    from sqlalchemy import create_engine, select

    from nemsei.db.session import build_session_factory
    from nemsei.portfolios.models import PortfolioMembership

    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session:
        # "Reporting Plant" is already an active member of this portfolio.
        membership_id = session.scalar(
            select(PortfolioMembership.id).where(PortfolioMembership.sub_account == "040")
        )
        asset_id = session.scalar(
            select(PortfolioMembership.asset_id).where(PortfolioMembership.asset_id.is_not(None)).limit(1)
        )

    form = client.get(f"/portfolios/{seeded}/members/review")
    response = client.post(
        f"/portfolios/{seeded}/members/{membership_id}/resolve",
        data={"csrf_token": csrf_token(form), "asset_id": str(asset_id), "next": "review"},
    )
    assert response.status_code == 302
    body = client.get(response.location).get_data(as_text=True)
    assert "already a member of this portfolio" in body

    with factory() as session:
        membership = session.get(PortfolioMembership, membership_id)
        # Untouched: the rejected resolution left no trace on the row.
        assert membership.resolution_state == "unresolved"
        assert membership.asset_id is None


def test_the_monthly_workflow_goes_through_generate_review_approve(app, seeded, settings) -> None:
    client = app.test_client()
    login(client)

    form = client.get(f"/portfolios/{seeded}/reports?month=2026-03")
    generate = client.post(
        f"/portfolios/{seeded}/reports/generate",
        data={"csrf_token": csrf_token(form), "month": "2026-03"},
    )
    assert generate.status_code == 302

    reports_page = client.get(f"/portfolios/{seeded}/reports?month=2026-03")
    body = reports_page.get_data(as_text=True)
    assert "Gerado" in body
    assert "Reporting Plant" in body  # the ready member, with its download links
    assert "Sem dados de produção no período" in body  # the blocked member, with a reason

    from sqlalchemy import create_engine, select

    from nemsei.db.session import build_session_factory
    from nemsei.portfolios.models import PortfolioReportRun

    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session:
        run_id = session.scalar(select(PortfolioReportRun.id).where(PortfolioReportRun.portfolio_id == seeded))

    review = client.post(
        f"/portfolios/{seeded}/reports/{run_id}/review",
        data={"csrf_token": csrf_token(reports_page), "month": "2026-03", "notes": "tudo bem"},
    )
    assert review.status_code == 302
    reports_page = client.get(f"/portfolios/{seeded}/reports?month=2026-03")

    approve = client.post(
        f"/portfolios/{seeded}/reports/{run_id}/approve",
        data={"csrf_token": csrf_token(reports_page), "month": "2026-03"},
    )
    assert approve.status_code == 302
    final_body = client.get(f"/portfolios/{seeded}/reports?month=2026-03").get_data(as_text=True)
    assert "Aprovado" in final_body
    assert "registo bloqueado" in final_body

    # And the database itself now refuses to touch the run.
    with factory() as session:
        from sqlalchemy import text

        with pytest.raises(Exception, match="cannot be changed"), session.begin():
            session.execute(text("UPDATE portfolio_report_runs SET review_notes = 'x' WHERE id = :r"), {"r": run_id})


def test_a_mutation_route_without_a_csrf_token_is_refused(app, seeded) -> None:
    client = app.test_client()
    login(client)
    response = client.post(f"/portfolios/{seeded}/reports/generate", data={"month": "2026-03"})
    assert response.status_code in (400, 403)


# --- D5: Portfolio Diagnostics ---------------------------------------------------


@pytest.fixture
def seeded_with_incident(app, settings):
    """A second portfolio, deliberately separate from `seeded`, with one
    real open critical incident and one real open warning incident on two
    different installations -- enough for the diagnostics tab and the
    Overview panel to have something real to render."""
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="Diagnostics Portfolio", created_by="op")

        broken = create_asset(session, canonical_name="Broken Plant")
        broken_device = create_device(session, asset_id=broken.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        session.add(
            DiagnosticIncident(
                rule_code="device_unavailable", asset_id=broken.id, device_id=broken_device.id, severity="critical",
                status="open", opened_at=datetime.now(timezone.utc), last_observed_at=datetime.now(timezone.utc),
                occurrence_count=1, detector_version="1", evidence_json={},
                created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            )
        )

        warning_asset = create_asset(session, canonical_name="Warning Plant")
        warning_device = create_device(session, asset_id=warning_asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        session.add(
            DiagnosticIncident(
                rule_code="stale_reading", asset_id=warning_asset.id, device_id=warning_device.id, severity="warning",
                status="open", opened_at=datetime.now(timezone.utc), last_observed_at=datetime.now(timezone.utc),
                occurrence_count=1, detector_version="1", evidence_json={},
                created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            )
        )

        healthy = create_asset(session, canonical_name="Healthy Plant")
        create_device(session, asset_id=healthy.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))

        for asset in (broken, warning_asset, healthy):
            add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        portfolio_id = portfolio.id
    return portfolio_id


def test_the_diagnostics_tab_shows_real_incidents_worst_first(app, seeded_with_incident) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded_with_incident}/diagnostics").get_data(as_text=True)
    assert "Broken Plant" in body
    assert "Warning Plant" in body
    assert "device_unavailable" in body
    assert "stale_reading" in body
    assert body.index("Broken Plant") < body.index("Warning Plant")


def test_the_diagnostics_severity_filter_does_not_hide_other_filter_options(app, seeded_with_incident) -> None:
    """Regression: filter options must come from the unfiltered set, or
    picking one filter hides the very options that could undo it. The
    installation ranking above the incident list is deliberately unaffected
    by this filter -- only the incident list itself narrows."""
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded_with_incident}/diagnostics?severity=critical").get_data(as_text=True)
    assert "device_unavailable" in body  # the one incident row that matches
    assert "<code>stale_reading</code>" not in body  # no incident row for the filtered-out one
    # But the rule dropdown must still offer stale_reading as an option,
    # even though no incident row on this filtered page has it.
    assert '<option value="stale_reading"' in body


def test_the_overview_shows_the_compact_diagnostics_panel(app, seeded_with_incident) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded_with_incident}/overview").get_data(as_text=True)
    assert "Diagnóstico" in body
    assert "Broken Plant" in body  # the priority installation appears on Overview too
    # The pre-existing reporting-completeness panel ("Composição") must
    # still be there, untouched -- this is an addition, not a replacement.
    assert "Composição" in body


def test_diagnostics_section_is_included_in_the_shared_navigation(app, seeded) -> None:
    client = app.test_client()
    login(client)
    body = client.get(f"/portfolios/{seeded}/overview?month=2026-03").get_data(as_text=True)
    assert f"/portfolios/{seeded}/diagnostics" in body
