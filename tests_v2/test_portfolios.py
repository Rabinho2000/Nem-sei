"""Portfolios: flat, temporal, frozen, and aggregated from the per-asset reports."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from nemsei.assets.service import create_asset, create_organization
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.portfolios.datasets import assets_needing_attention, build_portfolio_dataset
from nemsei.portfolios.models import PortfolioSnapshot
from nemsei.portfolios.service import (
    add_member,
    add_rule,
    create_portfolio,
    end_membership,
    freeze_snapshot,
    resolve_member_to_asset,
    resolve_members,
    slugify,
)
from nemsei.providers.service import create_connection, create_mapping


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


def make_asset(session, name: str, **kwargs):
    return create_asset(session, canonical_name=name, **kwargs)


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


def mapping_for(session, asset_id, key="c1", provider="fusionsolar"):
    connection = session.scalar(
        text("SELECT id FROM provider_connections WHERE connection_key = :k").bindparams(k=key)
    )
    if connection is None:
        connection = create_connection(
            session, provider_code=provider, connection_key=key, display_name=key,
            credential_reference="REF", enabled=True, configuration_status="configured",
        ).id
    return create_mapping(
        session, asset_id=asset_id, provider_connection_id=connection, external_id=f"EXT-{asset_id}"
    )


# --- shape ------------------------------------------------------------------


def test_a_portfolio_cannot_contain_another_portfolio(factory) -> None:
    """Stated by the absence of a column, and asserted so it stays absent."""
    with factory() as session:
        columns = {
            row[0]
            for row in session.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'portfolios'")
            )
        }
    forbidden = {"parent_id", "parent_portfolio_id", "portfolio_id"}
    assert not (columns & forbidden), f"portfolios must stay flat, found {columns & forbidden}"


def test_slugs_fold_accents_rather_than_dropping_them() -> None:
    assert slugify("Solcorélios I") == "solcorelios-i"
    assert slugify("  ") == "portfolio"


def test_a_portfolio_can_belong_to_a_customer(factory) -> None:
    with factory() as session, session.begin():
        owner = create_organization(session, display_name="Cliente A")
        portfolio = create_portfolio(session, name="Carteira A", owner_id=owner.id, created_by="operator")
        assert portfolio.owner_id == owner.id
        assert portfolio.slug == "carteira-a"


# --- membership -------------------------------------------------------------


def test_a_member_without_an_installation_is_still_a_member(factory) -> None:
    """23 of V1's 80 members are like this and carry real evidence."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="operator")
        membership = add_member(
            session, portfolio_id=portfolio.id, valid_from=date(2026, 1, 1), created_by="operator",
            sub_account="040", external_name="Consumidor Final", tax_id="999999990",
        )
        assert membership.resolution_state == "unresolved"
        assert membership.asset_id is None
        members = resolve_members(session, portfolio_id=portfolio.id, on=date(2026, 6, 1))
        assert len(members) == 1
        assert members[0].tax_id == "999999990"


def test_a_member_must_be_identifiable_as_something(factory) -> None:
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            portfolio = create_portfolio(session, name="P", created_by="operator")
            add_member(session, portfolio_id=portfolio.id, valid_from=date(2026, 1, 1), created_by="operator")


def test_one_asset_may_not_be_counted_twice_in_the_same_portfolio(factory) -> None:
    """Otherwise a plant is silently double-weighted in every total."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="operator")
        asset = make_asset(session, "Alpha")
        add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        ids = (portfolio.id, asset.id)
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            add_member(session, portfolio_id=ids[0], asset_id=ids[1], valid_from=date(2026, 6, 1), created_by="op")


def test_the_same_asset_may_belong_to_two_portfolios(factory) -> None:
    """V1 has two assets and four NIFs in both portfolios."""
    with factory() as session, session.begin():
        first = create_portfolio(session, name="Solcorelios I", created_by="op")
        second = create_portfolio(session, name="Solcorelios II", created_by="op")
        asset = make_asset(session, "Shared")
        add_member(session, portfolio_id=first.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        add_member(session, portfolio_id=second.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        assert len(resolve_members(session, portfolio_id=first.id, on=date(2026, 6, 1))) == 1
        assert len(resolve_members(session, portfolio_id=second.id, on=date(2026, 6, 1))) == 1


def test_membership_is_answerable_for_a_past_date(factory) -> None:
    """Which plants were in this portfolio in March, asked in December."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        stays, leaves = make_asset(session, "Stays"), make_asset(session, "Leaves")
        add_member(session, portfolio_id=portfolio.id, asset_id=stays.id, valid_from=date(2026, 1, 1), created_by="op")
        going = add_member(
            session, portfolio_id=portfolio.id, asset_id=leaves.id, valid_from=date(2026, 1, 1), created_by="op"
        )
        end_membership(session, membership_id=going.id, on=date(2026, 6, 1))

        march = resolve_members(session, portfolio_id=portfolio.id, on=date(2026, 3, 1))
        december = resolve_members(session, portfolio_id=portfolio.id, on=date(2026, 12, 1))
    assert len(march) == 2
    assert len(december) == 1


def test_resolving_a_member_takes_an_actor_and_never_happens_by_itself(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        asset = make_asset(session, "Discovered")
        member = add_member(
            session, portfolio_id=portfolio.id, valid_from=date(2026, 1, 1), created_by="op",
            external_name="A PIRES LOURENCO E FILHOS SA", tax_id="502265906",
        )
        assert member.resolution_state == "unresolved"
        resolve_member_to_asset(session, membership_id=member.id, asset_id=asset.id, resolved_by="sergio")
        assert member.resolution_state == "resolved"
        assert member.provenance_json["resolved_by"] == "sergio"


# --- rules ------------------------------------------------------------------


def test_rules_filter_assets_and_never_create_sub_portfolios(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="Portugal", created_by="op")
        make_asset(session, "PT one", country_code="PT")
        make_asset(session, "PT two", country_code="PT")
        make_asset(session, "ES one", country_code="ES")
        add_rule(session, portfolio_id=portfolio.id, attribute="country_code", values=["PT"], created_by="op")
        members = resolve_members(session, portfolio_id=portfolio.id, on=date(2026, 6, 1))
    assert len(members) == 2
    assert all(member.origin == "rule" for member in members)


def test_an_empty_rule_selects_nothing_rather_than_everything(factory) -> None:
    """An empty filter matching the whole estate is how a portfolio becomes the company."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        make_asset(session, "Anything", country_code="PT")
        with pytest.raises(ValueError, match="select nothing"):
            add_rule(session, portfolio_id=portfolio.id, attribute="country_code", values=[], created_by="op")


def test_an_explicit_member_is_not_duplicated_by_a_rule_that_also_selects_it(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        asset = make_asset(session, "Both", country_code="PT")
        add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        add_rule(session, portfolio_id=portfolio.id, attribute="country_code", values=["PT"], created_by="op")
        members = resolve_members(session, portfolio_id=portfolio.id, on=date(2026, 6, 1))
    assert len(members) == 1
    assert members[0].origin == "membership"


# --- snapshots --------------------------------------------------------------


def test_a_snapshot_freezes_the_exact_list_and_survives_a_later_change(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        a, b = make_asset(session, "A"), make_asset(session, "B")
        add_member(session, portfolio_id=portfolio.id, asset_id=a.id, valid_from=date(2026, 1, 1), created_by="op")
        second = add_member(
            session, portfolio_id=portfolio.id, asset_id=b.id, valid_from=date(2026, 1, 1), created_by="op"
        )
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        frozen = list(snapshot.asset_ids_json)
        end_membership(session, membership_id=second.id, on=date(2026, 4, 1))
        snapshot_id, ids = snapshot.id, (a.id, b.id)

    with factory() as session:
        stored = session.get(PortfolioSnapshot, snapshot_id)
    assert sorted(frozen) == sorted(ids)
    assert sorted(stored.asset_ids_json) == sorted(ids)


def test_refreezing_an_unchanged_membership_reuses_the_snapshot(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        asset = make_asset(session, "A")
        add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        first = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        second = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        assert first.id == second.id


def test_a_snapshot_cannot_be_changed_or_deleted(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        asset = make_asset(session, "A")
        add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        snapshot_id = snapshot.id
    for statement in (
        "UPDATE portfolio_snapshots SET created_by = 'x' WHERE id = :i",
        "DELETE FROM portfolio_snapshots WHERE id = :i",
    ):
        with pytest.raises(Exception, match="append-only"):
            with factory() as session, session.begin():
                session.execute(text(statement), {"i": snapshot_id})


# --- the dataset ------------------------------------------------------------


def test_the_portfolio_total_is_the_sum_of_the_individual_reports(factory) -> None:
    """If this disagrees with the per-asset datasets, the aggregate is wrong."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        totals = {}
        for name, value in (("A", "100"), ("B", "250")):
            asset = make_asset(session, name, installed_dc_power_kw=Decimal("10.5"))
            mapping = mapping_for(session, asset.id, key=f"c-{name}")
            add_production(session, asset.id, mapping.id, date(2026, 3, 10), value)
            add_member(
                session, portfolio_id=portfolio.id, asset_id=asset.id,
                valid_from=date(2026, 1, 1), created_by="op",
            )
            totals[name] = Decimal(value)
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        dataset = build_portfolio_dataset(session, snapshot=snapshot, built_by="op")

        assert Decimal(dataset.totals_json["production"]["value"]) == sum(totals.values())
        assert dataset.totals_json["production"]["state"] == "measured"
        assert dataset.coverage_json["label"] == "2/2"
        assert Decimal(dataset.totals_json["installed_dc_power_kw"]["value"]) == Decimal("21.0")
        # Every member names the per-asset dataset it came from.
        assert all(member.reporting_dataset_id is not None for member in dataset.members)


def test_a_missing_asset_makes_the_total_partial_rather_than_smaller(factory) -> None:
    """A smaller total that looks complete is the most expensive wrong number."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        measured = make_asset(session, "Measured")
        mapping = mapping_for(session, measured.id, key="c-m")
        add_production(session, measured.id, mapping.id, date(2026, 3, 10), "100")
        silent = make_asset(session, "Silent")
        for asset in (measured, silent):
            add_member(
                session, portfolio_id=portfolio.id, asset_id=asset.id,
                valid_from=date(2026, 1, 1), created_by="op",
            )
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        dataset = build_portfolio_dataset(session, snapshot=snapshot, built_by="op")

        assert Decimal(dataset.totals_json["production"]["value"]) == Decimal("100")
        assert dataset.totals_json["production"]["state"] == "partial"
        assert dataset.coverage_json["label"] == "1/2"
        assert dataset.coverage_json["assets_missing"] == 1


def test_performance_is_only_stated_when_both_sides_are_complete(factory) -> None:
    """A partial actual against a complete expected reads as underperformance."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        asset = make_asset(session, "A")
        mapping = mapping_for(session, asset.id, key="c-p")
        add_production(session, asset.id, mapping.id, date(2026, 3, 10), "100")
        add_member(
            session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op"
        )
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        dataset = build_portfolio_dataset(session, snapshot=snapshot, built_by="op")
        # No financial model, so expected is missing and performance is not claimed.
        assert dataset.totals_json["expected"]["state"] == "missing"
        assert dataset.totals_json["performance_pct"] is None


def test_the_overview_ranks_the_installations_needing_attention_first(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        good = make_asset(session, "Reporting")
        mapping = mapping_for(session, good.id, key="c-g")
        add_production(session, good.id, mapping.id, date(2026, 3, 10), "100")
        silent = make_asset(session, "Silent")
        for asset in (good, silent):
            add_member(
                session, portfolio_id=portfolio.id, asset_id=asset.id,
                valid_from=date(2026, 1, 1), created_by="op",
            )
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        dataset = build_portfolio_dataset(session, snapshot=snapshot, built_by="op")
        ranked = assets_needing_attention(session, dataset)

    assert ranked[0]["name"] == "Silent"
    assert ranked[0]["production_state"] == "missing"
    assert ranked[-1]["name"] == "Reporting"


def test_unresolved_members_are_counted_but_not_aggregated(factory) -> None:
    """They belong to the portfolio; they just have no installation to total."""
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        asset = make_asset(session, "Real")
        mapping = mapping_for(session, asset.id, key="c-u")
        add_production(session, asset.id, mapping.id, date(2026, 3, 10), "100")
        add_member(
            session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op"
        )
        add_member(
            session, portfolio_id=portfolio.id, valid_from=date(2026, 1, 1), created_by="op",
            sub_account="040", external_name="Consumidor Final", tax_id="999999990",
        )
        snapshot = freeze_snapshot(
            session, portfolio_id=portfolio.id, period_start=date(2026, 3, 1),
            period_end=date(2026, 4, 1), created_by="op",
        )
        dataset = build_portfolio_dataset(session, snapshot=snapshot, built_by="op")

        assert len(snapshot.members_json) == 2
        assert dataset.coverage_json["unresolved_members"] == 1
        assert dataset.coverage_json["assets_in_snapshot"] == 1
        assert Decimal(dataset.totals_json["production"]["value"]) == Decimal("100")
