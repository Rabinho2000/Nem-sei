"""Three different absences, kept apart, because they need three actions.

The old reporting screen could not distinguish "no data" from "no rates" from
"month not closed", which is why an operator could not use it to decide
anything. These pin that the distinction survives, and that the ordering puts
the rows worth opening at the top.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.models import Asset
from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.commercial import set_billing_config, set_tariff
from nemsei.reporting.readiness import (
    fleet_readiness,
    filter_readiness,
    month_bounds,
    sort_for_operator,
    summarise,
)


def utc(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


@pytest.fixture
def world(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        ids = {}
        for name, contract in (
            ("Esco Sem Dados", "ESCO"),
            ("Esco Sem Taxas", "ESCO"),
            ("Esco Sem Tarifa", "ESCO"),
            ("Esco Completo", "ESCO"),
            ("Epc Qualquer", "EPC"),
        ):
            asset = create_asset(session, canonical_name=name)
            asset.contract_type = contract
            session.flush()
            mapping = create_mapping(
                session, asset_id=asset.id, provider_connection_id=connection.id,
                external_id=f"NE={asset.id}",
            )
            ids[name] = (asset.id, mapping.id)
    return factory, ids


def add(session, asset_id, mapping_id, day: date, metric="production_energy", value="100", quality="complete"):
    record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"{metric}:{day.isoformat()}",
        period_start=utc(day),
        period_end=utc(date.fromordinal(day.toordinal() + 1)),
        granularity="day",
        metric_kind=metric,
        value=None if value is None else Decimal(value),
        unit="kWh",
        quality=quality,
        completeness="complete" if quality == "complete" else "partial",
        metadata={},
    )


def seed(factory, ids):
    with factory() as session, session.begin():
        # "Esco Sem Dados" gets nothing at all.
        for name in ("Esco Sem Taxas", "Esco Sem Tarifa", "Esco Completo", "Epc Qualquer"):
            asset_id, mapping_id = ids[name]
            for day in range(1, 21):
                add(session, asset_id, mapping_id, date(2026, 8, day))
        # Both have rates and a self-consumption figure; only one also has a
        # tariff in force, which the euro figures never depend on but the
        # tariff summary section of the report does.
        for name in ("Esco Sem Tarifa", "Esco Completo"):
            asset_id, mapping_id = ids[name]
            for day in range(1, 21):
                add(session, asset_id, mapping_id, date(2026, 8, day), "self_use_energy", "70")
            set_billing_config(
                session, asset_id=asset_id, report_type="esco", valid_from=date(2026, 1, 1),
                created_by="operador", solcor_price_per_kwh=Decimal("0.10"),
                default_electricity_price=Decimal("0.06"), default_export_price=Decimal("0.045"),
            )
        asset_id, _mapping_id = ids["Esco Completo"]
        set_tariff(
            session, asset_id=asset_id, tariff_type="simple", valid_from=date(2026, 1, 1),
            created_by="operador", prices={"simple": Decimal("0.18")},
        )


def by_name(rows):
    return {row.name: row for row in rows}


def test_the_three_absences_are_three_different_states(world) -> None:
    factory, ids = world
    seed(factory, ids)
    with factory() as session:
        rows = by_name(fleet_readiness(session, month="2026-08"))

    assert rows["Esco Sem Dados"].state == "blocked"
    assert rows["Esco Sem Taxas"].state == "needs_commercial"
    assert rows["Esco Completo"].state == "provisional"
    # An EPC is never held up for ESCO rates it does not charge.
    assert rows["Epc Qualquer"].state == "provisional"


def test_each_state_says_what_to_do_about_it(world) -> None:
    factory, ids = world
    seed(factory, ids)
    with factory() as session:
        rows = by_name(fleet_readiness(session, month="2026-08"))

    assert "Sem energia registada para o mês" in rows["Esco Sem Dados"].blockers
    assert "Sem taxas de venda / poupança / excedente" in rows["Esco Sem Taxas"].blockers
    assert "Faltam 11 de 31 dias" in rows["Esco Completo"].blockers


def test_a_missing_tariff_is_its_own_blocker_distinct_from_billing_config(world) -> None:
    """`AssetTariff` feeds the tariff summary, not the euro totals -- so this
    is a fourth, separate absence, not a rephrasing of "sem taxas"."""
    factory, ids = world
    seed(factory, ids)
    with factory() as session:
        rows = by_name(fleet_readiness(session, month="2026-08"))

    sem_tarifa, completo = rows["Esco Sem Tarifa"], rows["Esco Completo"]
    assert sem_tarifa.has_commercial is True
    assert sem_tarifa.has_tariff is False
    assert "Sem tarifa em vigor para o mês" in sem_tarifa.blockers
    assert "Sem taxas de venda / poupança / excedente" not in sem_tarifa.blockers
    # The euro figures themselves do not need a tariff at all.
    assert sem_tarifa.can_report_money is True
    assert completo.has_tariff is True
    assert "Sem tarifa em vigor para o mês" not in completo.blockers


def test_euros_need_both_the_rates_and_a_self_consumption_figure(world) -> None:
    """Neither substitutes for the other, and the screen must not imply it does."""
    factory, ids = world
    seed(factory, ids)
    with factory() as session:
        rows = by_name(fleet_readiness(session, month="2026-08"))

    assert rows["Esco Completo"].can_report_money is True
    # Rates but no self-use would be just as impossible; here it is the rates.
    assert rows["Esco Sem Taxas"].can_report_money is False
    assert "self_use" in rows["Esco Completo"].metrics_present
    assert "self_use" not in rows["Esco Sem Taxas"].metrics_present


def test_a_corrected_day_is_counted_once(world) -> None:
    """The dataset layer's rule, one level up: raw rows would double a day."""
    factory, ids = world
    asset_id, mapping_id = ids["Epc Qualquer"]
    with factory() as session, session.begin():
        add(session, asset_id, mapping_id, date(2026, 8, 1), value="100")
        add(session, asset_id, mapping_id, date(2026, 8, 1), value="90")
    with factory() as session:
        rows = by_name(fleet_readiness(session, month="2026-08"))
    assert rows["Epc Qualquer"].observed_days == 1


def test_a_day_persisted_as_missing_is_not_coverage(world) -> None:
    factory, ids = world
    asset_id, mapping_id = ids["Epc Qualquer"]
    with factory() as session, session.begin():
        add(session, asset_id, mapping_id, date(2026, 8, 1), value="100")
        add(session, asset_id, mapping_id, date(2026, 8, 2), value=None, quality="missing")
    with factory() as session:
        rows = by_name(fleet_readiness(session, month="2026-08"))
    assert rows["Epc Qualquer"].observed_days == 1
    assert rows["Epc Qualquer"].has_energy is True


def test_escos_come_first_and_within_them_the_worst_state(world) -> None:
    factory, ids = world
    seed(factory, ids)
    with factory() as session:
        rows = fleet_readiness(session, month="2026-08")

    names = [row.name for row in rows]
    assert names == ["Esco Sem Dados", "Esco Sem Taxas", "Esco Completo", "Esco Sem Tarifa", "Epc Qualquer"]
    # And the ordering is a property of the sort, not of the seeding order.
    assert [row.name for row in sort_for_operator(reversed(rows))] == names


def test_the_summary_counts_what_the_landing_page_leads_with(world) -> None:
    factory, ids = world
    seed(factory, ids)
    with factory() as session:
        summary = summarise(fleet_readiness(session, month="2026-08"))

    assert summary["total"] == 5
    assert summary["esco"] == 4
    assert summary["epc"] == 1
    assert summary["reportable"] == 4
    assert summary["without_energy"] == 1
    assert summary["esco_needs_commercial"] == 2
    assert summary["esco_needs_tariff"] == 1
    assert summary["money_possible"] == 2
    assert summary["final"] == 0


def test_the_filters_narrow_by_the_questions_the_screen_asks(world) -> None:
    factory, ids = world
    seed(factory, ids)
    with factory() as session:
        rows = fleet_readiness(session, month="2026-08")

    assert len(filter_readiness(rows, contract="esco")) == 4
    assert len(filter_readiness(rows, contract="epc")) == 1
    assert [row.name for row in filter_readiness(rows, state="blocked")] == ["Esco Sem Dados"]
    assert [row.name for row in filter_readiness(rows, state="needs_commercial")] == ["Esco Sem Taxas"]
    assert len(filter_readiness(rows, generated="no")) == 5
    assert [row.name for row in filter_readiness(rows, search="completo")] == ["Esco Completo"]


def test_a_month_with_no_data_anywhere_reports_zero_rather_than_failing(world) -> None:
    factory, _ids = world
    with factory() as session:
        summary = summarise(fleet_readiness(session, month="2025-01"))
    assert summary["reportable"] == 0
    assert summary["blocked"] == 5


def test_the_month_bounds_are_the_half_open_period_the_datasets_use() -> None:
    assert month_bounds("2026-08") == (date(2026, 8, 1), date(2026, 9, 1))
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))


def test_an_asset_stating_no_contract_is_not_counted_as_a_decided_epc(world) -> None:
    """`detect_report_type` answers EPC for silence; a count may not."""
    factory, ids = world
    with factory() as session, session.begin():
        session.get(Asset, ids["Epc Qualquer"][0]).contract_type = None
    with factory() as session:
        rows = by_name(fleet_readiness(session, month="2026-08"))
    assert rows["Epc Qualquer"].is_esco is False
    assert rows["Epc Qualquer"].contract_type is None
