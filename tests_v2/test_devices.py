from __future__ import annotations

from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from nemsei.assets.models import Device
from nemsei.assets.service import create_asset, create_device, normalize_serial_number
from nemsei.db.session import build_session_factory
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.repository import ProviderRepository
from nemsei.providers.service import create_connection, create_mapping, replace_mapping


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def factory_for(settings):
    return build_session_factory(create_engine(settings.database_url))


def enabled_connection(session, *, key: str = "fusionsolar-main"):
    return create_connection(
        session,
        provider_code="fusionsolar",
        connection_key=key,
        display_name="FusionSolar main",
        credential_reference="FUSIONSOLAR_MAIN",
        enabled=True,
        configuration_status="configured",
    )


def test_serial_normalization_folds_case_and_separators() -> None:
    assert normalize_serial_number(" bt2180195362 ") == "BT2180195362"
    assert normalize_serial_number("6T21-A901.0285") == "6T21A9010285"
    assert normalize_serial_number("   ") is None
    assert normalize_serial_number(None) is None


def test_serial_is_unique_per_asset_but_not_globally(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    with factory_for(settings)() as session, session.begin():
        first = create_asset(session, canonical_name="Alpha Solar")
        second = create_asset(session, canonical_name="Bravo Solar")
        create_device(session, asset_id=first.id, device_kind="inverter", serial_number="BT2180195362")

        # The same hardware serial on a different installation is legitimate.
        create_device(session, asset_id=second.id, device_kind="inverter", serial_number="bt2180195362")

        # A repeat within one installation is a review condition, not a merge.
        with pytest.raises(ValueError, match="already claimed"):
            create_device(session, asset_id=first.id, device_kind="inverter", serial_number="BT-2180195362")

        assert session.scalar(select(func.count()).select_from(Device)) == 2


def test_device_rejects_invalid_kind_power_and_foreign_parent(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    with factory_for(settings)() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        other = create_asset(session, canonical_name="Bravo Solar")
        foreign = create_device(session, asset_id=other.id, device_kind="datalogger")
        with pytest.raises(ValueError, match="Invalid device kind"):
            create_device(session, asset_id=asset.id, device_kind="turbine")
        with pytest.raises(ValueError, match="Rated power"):
            create_device(session, asset_id=asset.id, device_kind="inverter", rated_power_kw=Decimal("-1"))
        with pytest.raises(ValueError, match="same asset"):
            create_device(session, asset_id=asset.id, device_kind="inverter", parent_device_id=foreign.id)

        datalogger = create_device(session, asset_id=asset.id, device_kind="datalogger", label="Logger")
        inverter = create_device(session, asset_id=asset.id, device_kind="inverter", parent_device_id=datalogger.id)
        assert inverter.parent_device_id == datalogger.id


def test_device_mapping_requires_its_device_at_both_layers(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    with factory_for(settings)() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        other = create_asset(session, canonical_name="Bravo Solar")
        connection = enabled_connection(session)
        device = create_device(session, asset_id=asset.id, device_kind="inverter", serial_number="SN-1")
        foreign_device = create_device(session, asset_id=other.id, device_kind="inverter", serial_number="SN-2")

        with pytest.raises(ValueError, match="requires a device"):
            create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="dev-1", resource_kind="device")
        with pytest.raises(ValueError, match="cannot carry one"):
            create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="plant-1", device_id=device.id)
        with pytest.raises(ValueError, match="same asset"):
            create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="dev-2", resource_kind="device", device_id=foreign_device.id)

    # The database refuses the same inconsistency even without the service.
    with factory_for(settings)() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO asset_provider_mappings "
                    "(asset_id, provider_connection_id, resource_kind, external_id, normalized_external_id, mapping_status, valid_from, created_at, updated_at) "
                    "SELECT id, 1, 'device', 'raw-1', 'raw-1', 'pending_review', CURRENT_DATE, now(), now() FROM assets LIMIT 1"
                )
            )
        session.rollback()


def test_one_active_device_claim_per_connection_and_replacement_keeps_kind(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    with factory_for(settings)() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        other = create_asset(session, canonical_name="Bravo Solar")
        connection = enabled_connection(session)
        device = create_device(session, asset_id=asset.id, device_kind="inverter", serial_number="SN-1")
        rival = create_device(session, asset_id=other.id, device_kind="inverter", serial_number="SN-2")
        plant = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="station-1")
        mapping = create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id="1000000141941496",
            resource_kind="device",
            device_id=device.id,
            parent_mapping_id=plant.id,
        )
        assert mapping.parent_mapping_id == plant.id

        with pytest.raises(ValueError, match="Provider device is already actively mapped"):
            create_mapping(
                session,
                asset_id=other.id,
                provider_connection_id=connection.id,
                external_id="1000000141941496",
                resource_kind="device",
                device_id=rival.id,
            )

        # A plant may legitimately reuse the identifier: claims are per kind.
        create_mapping(session, asset_id=other.id, provider_connection_id=connection.id, external_id="1000000141941496")

        replacement = replace_mapping(session, mapping_id=mapping.id, replacement_external_id="1000000187304838")
        assert replacement.resource_kind == "device"
        assert replacement.device_id == device.id
        assert replacement.parent_mapping_id == plant.id
        assert session.get(AssetProviderMapping, mapping.id).mapping_status == "superseded"


def test_one_device_carries_identifiers_from_several_providers_over_time(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    with factory_for(settings)() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", serial_number="6T2159042269")
        huawei = enabled_connection(session)
        sigenergy = create_connection(
            session,
            provider_code="sigenergy",
            connection_key="sigenergy-main",
            display_name="Sigenergy main",
            credential_reference="SIGENERGY_MAIN",
            enabled=True,
            configuration_status="configured",
        )
        first = create_mapping(session, asset_id=asset.id, provider_connection_id=huawei.id, external_id="1000000139150452", resource_kind="device", device_id=device.id)
        second = create_mapping(session, asset_id=asset.id, provider_connection_id=sigenergy.id, external_id="SIG-DEV-77", resource_kind="device", device_id=device.id)

        # The hardware is one canonical device seen by two provider accounts.
        claims = session.scalars(select(AssetProviderMapping).where(AssetProviderMapping.device_id == device.id)).all()
        assert {claim.provider_connection_id for claim in claims} == {huawei.id, sigenergy.id}
        assert all(claim.mapping_status == "active" for claim in claims)

        # Superseding one provider identifier keeps the device and the history.
        replacement = replace_mapping(session, mapping_id=first.id, replacement_external_id="1000000187304838")
        superseded = session.get(AssetProviderMapping, first.id)
        assert superseded.mapping_status == "superseded"
        assert superseded.valid_to is not None
        assert superseded.replaced_by_mapping_id == replacement.id
        assert replacement.device_id == device.id
        assert session.get(AssetProviderMapping, second.id).mapping_status == "active"
        assert session.scalar(select(func.count()).select_from(Device)) == 1


def test_device_mappings_stay_out_of_plant_scoped_provider_selection(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    with factory_for(settings)() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        connection = enabled_connection(session)
        device = create_device(session, asset_id=asset.id, device_kind="inverter", serial_number="SN-1")
        plant = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="station-1")
        create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id="device-1",
            resource_kind="device",
            device_id=device.id,
            parent_mapping_id=plant.id,
        )
        repository = ProviderRepository(session)
        current = repository.current_mappings_for_connection(connection.id)
        historical = repository.mappings_for_connection_on_date(connection.id, plant.valid_from)
        assert [mapping.id for mapping in current] == [plant.id]
        assert [mapping.id for mapping in historical] == [plant.id]
