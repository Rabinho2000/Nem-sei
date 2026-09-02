"""Multiple simultaneous service engagements per installation.

Before this, `service_kind` had exactly one value, so nothing needed to
filter by it. These tests exist because the moment a second value became
possible, every unscoped read and write became a latent bug: recording an
ESCO engagement closing an unrelated open O&M one, or an ESCO period being
counted as O&M coverage. None of this has happened in production -- no ESCO
contract has ever been recorded -- but the scoping has to be proven before
the first one is.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from nemsei.assets.models import Asset
from nemsei.assets.service import create_asset
from nemsei.contracts.models import AssetServiceContract
from nemsei.contracts.service import (
    backfill_installation_ids,
    contracts_for,
    current_contract,
    esco_status_map,
    om_status_map,
    set_service_contract,
)
from nemsei.db.session import build_session_factory
from nemsei.installations.service import backfill_installations_from_assets
from nemsei.reporting.commercial import set_billing_config
from nemsei.reporting.commercial_models import AssetBillingConfig


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


# --- schema -----------------------------------------------------------------


def test_the_migration_widens_service_kind_and_adds_the_new_columns(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    columns = {c["name"] for c in inspect(engine).get_columns("asset_service_contracts")}
    assert "installation_id" in columns
    billing_columns = {c["name"] for c in inspect(engine).get_columns("asset_billing_configs")}
    assert "contract_id" in billing_columns


def test_the_migration_downgrades_cleanly(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    command.downgrade(Config("alembic.ini"), "0032_contract_scopes-1")
    engine = create_engine(settings.database_url)
    assert "installation_id" not in {c["name"] for c in inspect(engine).get_columns("asset_service_contracts")}
    assert "contract_id" not in {c["name"] for c in inspect(engine).get_columns("asset_billing_configs")}
    command.upgrade(Config("alembic.ini"), "head")


def test_an_unknown_service_kind_is_refused_by_the_database(factory) -> None:
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            asset = create_asset(session, canonical_name="Alpha")
            session.flush()
            from sqlalchemy import text

            session.execute(
                text(
                    "INSERT INTO asset_service_contracts (public_id, asset_id, service_kind, source_kind,"
                    " provenance_json, review_status, created_by, created_at, updated_at)"
                    " VALUES ('x', :asset_id, 'warranty', 'operator', '{}', 'clear', 'test', now(), now())"
                ),
                {"asset_id": asset.id},
            )


# --- the bug this milestone exists to prevent --------------------------------


def test_recording_an_esco_engagement_does_not_close_an_open_om_one(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="DIACO")
        asset_id = asset.id
        set_service_contract(
            session, asset_id=asset_id, created_by="op", valid_from=date(2020, 1, 1), service_kind="om"
        )

    with factory() as session, session.begin():
        set_service_contract(
            session, asset_id=asset_id, created_by="op", valid_from=date(2026, 1, 1), service_kind="esco"
        )

    with factory() as session:
        om = current_contract(session, asset_id=asset_id, service_kind="om", on=date(2026, 6, 1))
        esco = current_contract(session, asset_id=asset_id, service_kind="esco", on=date(2026, 6, 1))
        assert om is not None and om.valid_to is None
        assert esco is not None


def test_om_status_map_does_not_count_an_esco_period_as_om_coverage(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id
        set_service_contract(
            session, asset_id=asset_id, created_by="op", valid_from=date(2026, 1, 1), service_kind="esco"
        )

    with factory() as session:
        assert om_status_map(session, asset_ids=[asset_id])[asset_id]["status"] == "none"
        assert esco_status_map(session, asset_ids=[asset_id])[asset_id]["status"] == "active"


def test_two_esco_engagements_for_different_periods_can_have_different_terms(factory) -> None:
    """DIACO 2026-2030 at tariff X, DIACO 2031-2035 at tariff Y -- the case
    the whole `contract_id` link exists to make representable."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="DIACO")
        asset_id = asset.id
        first = set_service_contract(
            session,
            asset_id=asset_id,
            created_by="op",
            valid_from=date(2026, 1, 1),
            valid_to=date(2031, 1, 1),
            service_kind="esco",
        )
        set_billing_config(
            session,
            asset_id=asset_id,
            report_type="esco",
            valid_from=date(2026, 1, 1),
            created_by="op",
            solcor_price_per_kwh=Decimal("0.10"),
            contract_id=first.id,
        )
        first_id = first.id

    with factory() as session, session.begin():
        second = set_service_contract(
            session,
            asset_id=asset_id,
            created_by="op",
            valid_from=date(2031, 1, 1),
            service_kind="esco",
        )
        set_billing_config(
            session,
            asset_id=asset_id,
            report_type="esco",
            valid_from=date(2031, 1, 1),
            created_by="op",
            solcor_price_per_kwh=Decimal("0.15"),
            contract_id=second.id,
        )
        second_id = second.id

    with factory() as session:
        configs = {
            config.contract_id: config.solcor_price_per_kwh
            for config in session.scalars(select(AssetBillingConfig).where(AssetBillingConfig.asset_id == asset_id))
        }
        assert configs[first_id] == Decimal("0.10")
        assert configs[second_id] == Decimal("0.15")


def test_the_om_history_table_does_not_show_esco_periods(factory) -> None:
    """The exact regression `asset_contract_panel` would have had: before the
    fix, `contracts_for(session, asset_id=asset_id)` returned every kind."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id
        set_service_contract(session, asset_id=asset_id, created_by="op", valid_from=date(2020, 1, 1), service_kind="om")
        set_service_contract(session, asset_id=asset_id, created_by="op", valid_from=date(2026, 1, 1), service_kind="esco")

    with factory() as session:
        om_only = contracts_for(session, asset_id=asset_id, service_kind="om")
        everything = contracts_for(session, asset_id=asset_id)
        assert len(om_only) == 1 and om_only[0].service_kind == "om"
        assert len(everything) == 2


# --- installation_id ----------------------------------------------------


def test_a_new_contract_is_linked_to_the_asset_installation_immediately(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id
        backfill_installations_from_assets(session)

    with factory() as session, session.begin():
        contract = set_service_contract(session, asset_id=asset_id, created_by="op", valid_from=date(2026, 1, 1))
        asset = session.get(Asset, asset_id)
        assert contract.installation_id == asset.installation_id
        assert contract.installation_id is not None


def test_a_contract_created_before_the_installation_backfill_gets_none(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id

    with factory() as session, session.begin():
        contract = set_service_contract(session, asset_id=asset_id, created_by="op", valid_from=date(2026, 1, 1))
        assert contract.installation_id is None


def test_backfill_installation_ids_links_existing_contracts_after_the_fact(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id
        contract = set_service_contract(session, asset_id=asset_id, created_by="op", valid_from=date(2026, 1, 1))
        contract_id = contract.id

    with factory() as session, session.begin():
        backfill_installations_from_assets(session)

    with factory() as session, session.begin():
        result = backfill_installation_ids(session)

    assert result["updated"] == 1 and result["no_installation"] == 0
    with factory() as session:
        contract = session.get(AssetServiceContract, contract_id)
        asset = session.get(Asset, asset_id)
        assert contract.installation_id == asset.installation_id


def test_backfill_installation_ids_is_safe_to_run_twice(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        set_service_contract(session, asset_id=asset.id, created_by="op", valid_from=date(2026, 1, 1))
        backfill_installations_from_assets(session)

    with factory() as session, session.begin():
        first = backfill_installation_ids(session)
    with factory() as session, session.begin():
        second = backfill_installation_ids(session)

    assert first["updated"] == 1
    assert second["updated"] == 0


# --- what it must not touch --------------------------------------------------


def test_setting_an_esco_contract_leaves_the_one_real_billing_config_alone(factory) -> None:
    """The one production `AssetBillingConfig` row predates `contract_id` and
    must keep resolving exactly as before."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha")
        asset_id = asset.id
        set_billing_config(
            session,
            asset_id=asset_id,
            report_type="esco",
            valid_from=date(2020, 1, 1),
            created_by="op",
        )

    with factory() as session:
        from nemsei.reporting.commercial import resolve_billing_config

        config = resolve_billing_config(session, asset_id=asset_id, on=date(2026, 1, 1))
        assert config is not None
        assert config.contract_id is None
