"""The operational read-model: enrichment on top of `queries.list_assets_data`,
the fault/communication/coverage split honoured everywhere, and graceful
behaviour before the Installation backfill has run in production -- which is
the real state of production today.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.installations.service import backfill_installations_from_assets, installation_for_asset
from nemsei.shared.clock import utc_now
from nemsei.web.installation_queries import (
    incident_counts_by_category,
    installation_detail,
    installation_list_rows,
)
from nemsei.web.work_order_queries import overdue_and_unscheduled_counts
from nemsei.work_orders.service import create_work_order


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def seed_incident(session, *, asset_id: int, rule_code: str, severity: str = "warning") -> int:
    now = utc_now()
    incident = DiagnosticIncident(
        rule_code=rule_code, asset_id=asset_id, severity=severity, status="open",
        opened_at=now, last_observed_at=now, detector_version="test", created_at=now, updated_at=now,
    )
    session.add(incident)
    session.flush()
    return incident.id


# --- incident_counts_by_category -----------------------------------------


def test_coverage_incidents_never_count_as_fault(factory) -> None:
    """The whole reason this module exists: `stale_reading` (94% of the real
    backlog) must never inflate the fault count."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        seed_incident(session, asset_id=asset.id, rule_code="stale_reading")
        seed_incident(session, asset_id=asset.id, rule_code="device_no_history")
        asset_id = asset.id

    with factory() as session:
        counts = incident_counts_by_category(session, asset_ids=[asset_id])[asset_id]

    assert counts["fault"] == 0
    assert counts["coverage"] == 2
    assert counts["total"] == 2


def test_fault_and_coverage_are_counted_independently(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        seed_incident(session, asset_id=asset.id, rule_code="device_unavailable", severity="critical")
        seed_incident(session, asset_id=asset.id, rule_code="stale_reading")
        seed_incident(session, asset_id=asset.id, rule_code="plant_offline", severity="critical")
        asset_id = asset.id

    with factory() as session:
        counts = incident_counts_by_category(session, asset_ids=[asset_id])[asset_id]

    assert counts == {"fault": 1, "communication": 1, "coverage": 1, "total": 3, "labels": counts["labels"], "tones": counts["tones"]}


def test_an_asset_with_no_incidents_gets_all_zeros_not_a_missing_key(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Quiet Plant")
        asset_id = asset.id

    with factory() as session:
        counts = incident_counts_by_category(session, asset_ids=[asset_id])[asset_id]

    assert counts["fault"] == 0 and counts["communication"] == 0 and counts["coverage"] == 0


def test_a_resolved_incident_does_not_count(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        now = utc_now()
        session.add(
            DiagnosticIncident(
                rule_code="device_unavailable", asset_id=asset.id, severity="critical", status="resolved",
                opened_at=now, last_observed_at=now, resolved_at=now, detector_version="test",
                created_at=now, updated_at=now,
            )
        )
        asset_id = asset.id

    with factory() as session:
        counts = incident_counts_by_category(session, asset_ids=[asset_id])[asset_id]

    assert counts["total"] == 0


# --- installation_list_rows -----------------------------------------------


def test_the_list_enriches_every_row_with_incidents_esco_work_and_priority(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        seed_incident(session, asset_id=asset.id, rule_code="device_unavailable", severity="critical")

    with factory() as session:
        result = installation_list_rows(session)

    assert len(result["assets"]) == 1
    row = result["assets"][0]
    assert row["incidents"]["fault"] == 1
    assert "esco" in row
    assert "work" in row
    assert "priority_rank" in row and "priority_reason" in row


def test_a_real_fault_ranks_before_a_communication_problem_in_the_list(factory) -> None:
    with factory() as session, session.begin():
        fault_asset = create_asset(session, canonical_name="Com Avaria")
        seed_incident(session, asset_id=fault_asset.id, rule_code="device_unavailable", severity="critical")
        comm_asset = create_asset(session, canonical_name="Sem Comunicacao")
        seed_incident(session, asset_id=comm_asset.id, rule_code="plant_offline", severity="critical")

    with factory() as session:
        result = installation_list_rows(session)

    by_name = {row["canonical_name"]: row for row in result["assets"]}
    assert by_name["Com Avaria"]["priority_rank"] < by_name["Sem Comunicacao"]["priority_rank"]


# --- installation_detail: header and the pre-backfill degraded state ------


def test_detail_before_the_backfill_has_run_still_renders_honestly(factory) -> None:
    """The real state of production today: `installations` is empty. The
    detail page must render, not 500, and must say clearly that the
    Installation-scoped features are not available yet -- not hide them
    silently."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="resumo")

    assert detail is not None
    assert detail["has_installation"] is False
    assert detail["installation"] is None
    assert detail["coordinates_known"] is False
    assert detail["production_window"].state == "unknown"


def test_the_trabalhos_tab_is_explicitly_blocked_without_an_installation(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="trabalhos")

    assert detail["blocked"] is True
    assert detail["work_orders"] == []


def test_the_timeline_tab_is_explicitly_blocked_without_an_installation(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="timeline")

    assert detail["blocked"] is True
    assert detail["events"] == []


def test_an_unknown_asset_returns_none_not_an_exception(factory) -> None:
    with factory() as session:
        assert installation_detail(session, asset_id=999999) is None


def test_an_unknown_tab_falls_back_to_resumo(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="nonsense")

    assert detail["tab"] == "resumo"


# --- installation_detail: once the backfill has run ------------------------


def test_the_trabalhos_tab_works_once_the_installation_exists(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        backfill_installations_from_assets(session)
        installation_id = installation_for_asset(session, asset_id=asset.id).id
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Reparar", created_by="op"
        )
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="trabalhos")

    assert detail["blocked"] is False
    assert len(detail["work_orders"]) == 1


def test_the_equipamentos_tab_lists_devices_and_names_module_groups_as_unavailable(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="equipamentos")

    assert len(detail["inverters"]) == 1
    assert detail["module_groups_available"] is False


def test_the_performance_tab_is_an_honest_empty_state_without_production_data(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="performance")

    assert detail["has_production_data"] is False
    assert detail["billing"] is None


def test_the_operacao_tab_splits_open_incidents_by_category(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        seed_incident(session, asset_id=asset.id, rule_code="device_unavailable", severity="critical")
        seed_incident(session, asset_id=asset.id, rule_code="stale_reading")
        asset_id = asset.id

    with factory() as session:
        detail = installation_detail(session, asset_id=asset_id, tab="operacao")

    assert len(detail["incidents_by_category"]["fault"]) == 1
    assert len(detail["incidents_by_category"]["coverage"]) == 1
    assert detail["total_open"] == 2


# --- work_order_queries -----------------------------------------------


def test_overdue_and_unscheduled_counts_are_zero_without_an_installation(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session:
        counts = overdue_and_unscheduled_counts(session, asset_ids=[asset_id])

    assert counts[asset_id] == {"overdue": 0, "unscheduled": 0}


def test_overdue_and_unscheduled_counts_once_work_orders_exist(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        backfill_installations_from_assets(session)
        installation_id = installation_for_asset(session, asset_id=asset.id).id
        create_work_order(
            session, installation_id=installation_id, work_type="corrective", title="Atrasado",
            created_by="op", due_date=date.today() - timedelta(days=1),
        )
        create_work_order(
            session, installation_id=installation_id, work_type="preventive", title="Sem data", created_by="op"
        )
        asset_id = asset.id

    with factory() as session:
        counts = overdue_and_unscheduled_counts(session, asset_ids=[asset_id])

    assert counts[asset_id] == {"overdue": 1, "unscheduled": 2}
