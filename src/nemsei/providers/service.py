"""Validation and historical transition rules for provider mappings."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from nemsei.assets.repository import AssetRepository
from nemsei.providers.models import (
    CONNECTION_STATUSES,
    MAPPING_STATUSES,
    AssetProviderMapping,
    ProviderConnection,
)
from nemsei.providers.registry import ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now


def create_connection(
    session: Session,
    *,
    provider_code: ProviderCode | str,
    connection_key: str,
    display_name: str,
    account_reference: str | None = None,
    region: str | None = None,
    credential_reference: str | None = None,
    enabled: bool = False,
    configuration_status: str = "not_configured",
) -> ProviderConnection:
    provider = ProviderCode(provider_code).value if isinstance(provider_code, ProviderCode) else ProviderCode(provider_code.lower()).value
    key = connection_key.strip()
    if not key or not display_name.strip():
        raise ValueError("Provider connection key and name are required.")
    if configuration_status not in CONNECTION_STATUSES:
        raise ValueError("Invalid provider connection configuration status.")
    now = utc_now()
    connection = ProviderConnection(
        provider_code=provider,
        connection_key=key,
        display_name=display_name.strip(),
        account_reference=account_reference.strip() if account_reference else None,
        region=region.strip() if region else None,
        credential_reference=credential_reference.strip() if credential_reference else None,
        enabled=enabled,
        configuration_status=configuration_status,
        created_at=now,
        updated_at=now,
    )
    ProviderRepository(session).add(connection)
    session.flush()
    return connection


def create_mapping(
    session: Session,
    *,
    asset_id: int,
    provider_connection_id: int,
    external_id: str,
    external_name: str | None = None,
    valid_from: date | None = None,
    monitoring_priority: int | None = None,
    production_priority: int | None = None,
    mapping_status: str = "active",
    notes: str | None = None,
) -> AssetProviderMapping:
    assets = AssetRepository(session)
    providers = ProviderRepository(session)
    if assets.asset(asset_id) is None:
        raise ValueError("Unknown asset.")
    connection = providers.connection(provider_connection_id)
    if connection is None:
        raise ValueError("Unknown provider connection.")
    if mapping_status not in MAPPING_STATUSES:
        raise ValueError("Invalid provider mapping status.")
    external_id = external_id.strip()
    if not external_id:
        raise ValueError("Provider external ID is required.")
    if mapping_status == "active":
        claimed = providers.active_external_claim(
            connection_id=connection.id,
            normalized_external_id=normalize_external_id(connection.provider_code, external_id),
        )
        if claimed is not None:
            raise ValueError("Provider plant is already actively mapped to another asset.")
    if any(priority is not None and priority <= 0 for priority in (monitoring_priority, production_priority)):
        raise ValueError("Source priorities must be positive.")
    now = utc_now()
    mapping = AssetProviderMapping(
        asset_id=asset_id,
        provider_connection_id=connection.id,
        resource_kind="plant",
        external_id=external_id,
        normalized_external_id=normalize_external_id(connection.provider_code, external_id),
        external_name=external_name.strip() if external_name else None,
        mapping_status=mapping_status,
        valid_from=valid_from or now.date(),
        monitoring_priority=monitoring_priority,
        production_priority=production_priority,
        notes=notes.strip() if notes else None,
        created_at=now,
        updated_at=now,
    )
    providers.add(mapping)
    session.flush()
    return mapping


def replace_mapping(
    session: Session,
    *,
    mapping_id: int,
    replacement_external_id: str,
    replacement_external_name: str | None = None,
    effective_on: date | None = None,
) -> AssetProviderMapping:
    providers = ProviderRepository(session)
    previous = providers.mapping(mapping_id)
    if previous is None or previous.mapping_status != "active" or previous.valid_to is not None:
        raise ValueError("Only an active mapping can be replaced.")
    effective = effective_on or utc_now().date()
    if effective < previous.valid_from:
        raise ValueError("Replacement date cannot precede the original mapping.")
    replacement = create_mapping(
        session,
        asset_id=previous.asset_id,
        provider_connection_id=previous.provider_connection_id,
        external_id=replacement_external_id,
        external_name=replacement_external_name,
        valid_from=effective,
        monitoring_priority=previous.monitoring_priority,
        production_priority=previous.production_priority,
    )
    previous.mapping_status = "superseded"
    previous.valid_to = effective
    previous.replaced_by_mapping_id = replacement.id
    previous.updated_at = utc_now()
    session.flush()
    return replacement
