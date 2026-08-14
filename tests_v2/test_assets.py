from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from nemsei.assets.repository import AssetRepository
from nemsei.assets.service import add_alias, create_asset, create_organization
from nemsei.db import build_engine, build_session_factory
from nemsei.providers.service import cross_connection_conflicts, create_connection, create_mapping, replace_mapping
from tests_v2.test_migrations import upgrade


def session_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))()


def test_asset_names_are_not_a_false_global_identity(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        first = create_asset(session, canonical_name="Central A")
        second = create_asset(session, canonical_name="Central A")
        session.commit()
        assert first.id != second.id


def test_organization_tax_id_is_unique_when_present(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        create_organization(session, display_name="Owner A", tax_id="PT 501-123-456")
        session.commit()
        with pytest.raises(IntegrityError):
            create_organization(session, display_name="Owner B", tax_id="501123456")


def test_aliases_can_be_ambiguous_between_assets(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        first = create_asset(session, canonical_name="Central A")
        second = create_asset(session, canonical_name="Central B")
        add_alias(session, asset_id=first.id, alias="Shared")
        add_alias(session, asset_id=second.id, alias="Shared")
        session.commit()


def test_provider_external_id_has_one_active_connection_scoped_claim(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        first = create_asset(session, canonical_name="Central A")
        second = create_asset(session, canonical_name="Central B")
        connection = create_connection(session, provider_code="sigenergy", connection_key="account-a", display_name="Account A")
        create_mapping(session, asset_id=first.id, provider_connection_id=connection.id, external_id="SIG-1")
        with pytest.raises(ValueError, match="already actively mapped"):
            create_mapping(session, asset_id=second.id, provider_connection_id=connection.id, external_id="sig-1")


def test_same_provider_id_can_remain_distinct_across_connections(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        first = create_asset(session, canonical_name="Central A")
        second = create_asset(session, canonical_name="Central B")
        account_a = create_connection(session, provider_code="sigenergy", connection_key="account-a", display_name="Account A")
        account_b = create_connection(session, provider_code="sigenergy", connection_key="account-b", display_name="Account B")
        mapping = create_mapping(session, asset_id=first.id, provider_connection_id=account_a.id, external_id="SIG-1")
        other = create_mapping(session, asset_id=second.id, provider_connection_id=account_b.id, external_id="SIG-1")
        assert [conflict.id for conflict in cross_connection_conflicts(session, mapping_id=mapping.id)] == [other.id]
        session.commit()


def test_mapping_replacement_preserves_history(settings, monkeypatch) -> None:
    with session_for(settings, monkeypatch) as session:
        asset = create_asset(session, canonical_name="Central A")
        connection = create_connection(session, provider_code="fusionsolar", connection_key="account-a", display_name="Account A")
        original = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="plant-1", valid_from=date(2026, 1, 1))
        replacement = replace_mapping(session, mapping_id=original.id, replacement_external_id="plant-2", effective_on=date(2026, 2, 1))
        session.commit()
        assert original.mapping_status == "superseded"
        assert original.valid_to == date(2026, 2, 1)
        assert original.replaced_by_mapping_id == replacement.id
        assert AssetRepository(session).asset(asset.id)
