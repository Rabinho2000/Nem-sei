"""ESCO first: what Solcor sells decides what gets fixed first.

Under an ESCO the installation is Solcor's and Solcor sells the energy, so an
hour of downtime is Solcor's lost revenue. These prove the classification, the
ordering rule it drives, and the one thing that ordering must never do.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.contracts.priority import commercial_family, describe, service_priority
from nemsei.contracts.service import set_service_contract
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.web.diagnostics_queries import open_incidents_overview
from tests_v2.test_migrations import upgrade

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "contract_type,expected",
    [
        ("ESCO", "esco"),
        ("EPC", "epc"),
        ("EPC (O&M)", "epc"),
        # A bought-out system is no longer Solcor's to lose revenue on, so it
        # is deliberately not `esco` here -- unlike `detect_report_type_value`,
        # which must still send it an ESCO-shaped report.
        ("ESCO BUYOUT", "esco_buyout"),
        ("esco buyout", "esco_buyout"),
        ("", "unknown"),
        (None, "unknown"),
        # V1's `asset_type` values leak into this column on a few rows; an
        # unrecognised word is not quietly called EPC.
        ("Tarifa", "unknown"),
    ],
)
def test_commercial_family_reads_v1s_free_text(contract_type, expected):
    assert commercial_family(contract_type) == expected


def test_priority_needs_both_an_esco_and_a_live_contract():
    assert service_priority(family="esco", om_status="active") == "high"
    assert service_priority(family="esco", om_status="undated") == "high"
    # Same commercial arrangement, no live O&M: nobody is paying Solcor to
    # operate it, so it is not the thing to fix first.
    assert service_priority(family="esco", om_status="expired") == "low"
    assert service_priority(family="esco", om_status="none") == "low"
    assert service_priority(family="epc", om_status="active") == "normal"
    assert service_priority(family="esco_buyout", om_status="active") == "normal"


def test_the_reason_travels_with_the_priority():
    described = describe("ESCO", "active")
    assert described["priority"] == "high"
    assert "receita perdida" in described["priority_reason"]
    assert described["contract_type"] == "ESCO"


# --- the ordering it drives ------------------------------------------------


def seeded(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    ids = {}
    plan = [
        ("esco_warning", "ESCO", True, "warning"),
        ("epc_warning", "EPC", True, "warning"),
        ("esco_critical", "ESCO", True, "critical"),
        ("epc_critical", "EPC", True, "critical"),
        ("esco_sem_om", "ESCO", False, "critical"),
    ]
    for label, contract_type, with_om, severity in plan:
        asset = create_asset(session, canonical_name=f"Central {label}")
        # `contract_type` has no setter on `create_asset`: it arrives from V1
        # via `import_v1_commercial_terms`, which writes the column directly.
        asset.contract_type = contract_type
        session.flush()
        ids[label] = asset.id
        if with_om:
            set_service_contract(
                session, asset_id=asset.id, created_by="importer",
                valid_from=TODAY - timedelta(days=100), valid_to=TODAY + timedelta(days=100),
            )
        session.add(
            DiagnosticIncident(
                rule_code="plant_offline" if severity == "critical" else "stale_reading",
                asset_id=asset.id, device_id=None, severity=severity, status="open",
                opened_at=NOW - timedelta(hours=1), last_observed_at=NOW, resolved_at=None,
                occurrence_count=1, detector_version="1", evidence_json={},
                created_at=NOW, updated_at=NOW,
            )
        )
    session.commit()
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"] = True, "admin"
    return client, ids


def order_of(session, **kwargs) -> list[int]:
    return [row["asset"].id for row in open_incidents_overview(session, **kwargs)]


def test_a_plant_that_is_down_is_never_buried_under_an_esco_warning(settings, monkeypatch):
    """The one ordering this page must never produce."""
    _, ids = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    order = order_of(session, om="todos")
    assert order.index(ids["epc_critical"]) < order.index(ids["esco_warning"])
    session.close()


def test_esco_sorts_first_inside_the_same_severity(settings, monkeypatch):
    _, ids = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    order = order_of(session, om="todos")
    assert order.index(ids["esco_critical"]) < order.index(ids["epc_critical"])
    assert order.index(ids["esco_warning"]) < order.index(ids["epc_warning"])
    session.close()


def test_an_esco_without_om_loses_its_priority(settings, monkeypatch):
    """Priority is what Solcor is paid to operate, not the contract's name."""
    _, ids = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    order = order_of(session, om="todos")
    assert order.index(ids["esco_critical"]) < order.index(ids["esco_sem_om"])
    session.close()


def test_the_incidents_page_defaults_to_the_om_portfolio(settings, monkeypatch):
    _, ids = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    assert ids["esco_sem_om"] not in order_of(session)
    assert ids["esco_sem_om"] in order_of(session, om="todos")
    assert order_of(session, om="nao") == [ids["esco_sem_om"]]
    session.close()


def test_the_family_filter_narrows_to_esco(settings, monkeypatch):
    _, ids = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    esco = order_of(session, family="esco", om="todos")
    assert set(esco) == {ids["esco_critical"], ids["esco_warning"], ids["esco_sem_om"]}
    session.close()


# --- the screens -----------------------------------------------------------


def test_the_incidents_page_shows_priority_and_the_way_out(settings, monkeypatch):
    client, _ = seeded(settings, monkeypatch)
    page = client.get("/diagnostics/incidents").get_data(as_text=True)
    assert "Prioritária" in page
    assert "Parque O&amp;M" in page
    assert "Ver todo o parque" in page


def test_the_assets_list_defaults_to_the_om_portfolio_and_says_so(settings, monkeypatch):
    client, ids = seeded(settings, monkeypatch)
    page = client.get("/assets").get_data(as_text=True)
    assert "a mostrar o parque O&amp;M" in page
    assert "Central esco_sem_om" not in page
    everything = client.get("/assets?om=todos").get_data(as_text=True)
    assert "Central esco_sem_om" in everything


def test_the_assets_list_names_the_contract_type(settings, monkeypatch):
    client, _ = seeded(settings, monkeypatch)
    page = client.get("/assets").get_data(as_text=True)
    assert ">ESCO</span>" in page
    assert ">EPC</span>" in page
    assert "prioritária" in page


def test_the_dashboard_leads_with_esco(settings, monkeypatch):
    client, _ = seeded(settings, monkeypatch)
    page = client.get("/").get_data(as_text=True)
    assert "ESCO com problema" in page
    # Two ESCO installations under a live contract, both with an open incident.
    assert "de 2 ESCO em contrato" in page
