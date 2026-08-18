"""Validation and historical transition rules for provider mappings."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from nemsei.assets.models import Asset
from nemsei.assets.repository import AssetRepository
from nemsei.providers.audit import record_operator_action
from nemsei.providers.models import (
    CONNECTION_STATUSES,
    MAPPING_STATUSES,
    AssetProviderMapping,
    ProviderConnection,
    LegacyImportRecord,
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


def configure_connection(
    session: Session,
    *,
    connection_id: int,
    display_name: str | None = None,
    account_reference: str | None = None,
    region: str | None = None,
    credential_reference: str | None = None,
    actor_username: str | None = None,
) -> ProviderConnection:
    connection = session.execute(
        select(ProviderConnection).where(ProviderConnection.id == connection_id).with_for_update()
    ).scalar_one_or_none()
    if connection is None:
        raise ValueError("Unknown provider connection.")
    if credential_reference is not None:
        reference = credential_reference.strip()
        if not reference or len(reference) > 255 or any(char.isspace() for char in reference):
            raise ValueError("Credential reference must be a non-empty secret reference name.")
        connection.credential_reference = reference
    if display_name is not None:
        if not display_name.strip():
            raise ValueError("Connection name is required.")
        connection.display_name = display_name.strip()
    connection.account_reference = account_reference.strip() if account_reference and account_reference.strip() else None
    connection.region = region.strip() if region and region.strip() else None
    if connection.credential_reference:
        connection.configuration_status = "configured"
    connection.updated_at = utc_now()
    if actor_username:
        record_operator_action(
            session,
            actor_username=actor_username,
            action="connection_configured",
            entity_type="provider_connection",
            entity_id=connection.id,
            metadata={"provider_code": connection.provider_code, "connection_id": connection.id},
        )
    session.flush()
    return connection


def set_connection_enabled(
    session: Session,
    *,
    connection_id: int,
    enabled: bool,
    actor_username: str,
) -> ProviderConnection:
    connection = session.execute(
        select(ProviderConnection).where(ProviderConnection.id == connection_id).with_for_update()
    ).scalar_one_or_none()
    if connection is None:
        raise ValueError("Unknown provider connection.")
    if enabled and (connection.configuration_status == "not_configured" or not connection.credential_reference):
        raise ValueError("Configure a credential reference before enabling the connection.")
    connection.enabled = enabled
    connection.configuration_status = "configured" if enabled else "disabled"
    connection.updated_at = utc_now()
    record_operator_action(
        session,
        actor_username=actor_username,
        action="connection_enabled" if enabled else "connection_disabled",
        entity_type="provider_connection",
        entity_id=connection.id,
        metadata={"provider_code": connection.provider_code, "connection_id": connection.id},
    )
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
    valid_to: date | None = None,
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
        if not connection.enabled or connection.configuration_status != "configured":
            raise ValueError("An active mapping requires an enabled, configured provider connection.")
        claimed = providers.active_external_claim(
            connection_id=connection.id,
            normalized_external_id=normalize_external_id(connection.provider_code, external_id),
        )
        if claimed is not None:
            raise ValueError("Provider plant is already actively mapped to another asset.")
    if any(priority is not None and priority <= 0 for priority in (monitoring_priority, production_priority)):
        raise ValueError("Source priorities must be positive.")
    now = utc_now()
    starts = valid_from or now.date()
    if valid_to is not None and valid_to < starts:
        raise ValueError("Mapping valid_to cannot precede valid_from.")
    mapping = AssetProviderMapping(
        asset_id=asset_id,
        provider_connection_id=connection.id,
        resource_kind="plant",
        external_id=external_id,
        normalized_external_id=normalize_external_id(connection.provider_code, external_id),
        external_name=external_name.strip() if external_name else None,
        mapping_status=mapping_status,
        valid_from=starts,
        valid_to=valid_to,
        monitoring_priority=monitoring_priority,
        production_priority=production_priority,
        notes=notes.strip() if notes else None,
        created_at=now,
        updated_at=now,
    )
    providers.add(mapping)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("Provider mapping conflict.") from exc
    return mapping


def approve_mapping(session: Session, *, mapping_id: int, actor_username: str) -> AssetProviderMapping:
    mapping = session.execute(
        select(AssetProviderMapping).where(AssetProviderMapping.id == mapping_id).with_for_update()
    ).scalar_one_or_none()
    if mapping is None:
        raise ValueError("Unknown provider mapping.")
    if mapping.mapping_status == "active":
        return mapping
    if mapping.mapping_status != "pending_review":
        raise ValueError("Only pending mappings can be approved.")
    asset = session.execute(select(Asset).where(Asset.id == mapping.asset_id).with_for_update()).scalar_one_or_none()
    connection = session.execute(
        select(ProviderConnection).where(ProviderConnection.id == mapping.provider_connection_id).with_for_update()
    ).scalar_one_or_none()
    if asset is None or connection is None:
        raise ValueError("Mapping asset and provider connection are required.")
    if asset.review_status == "needs_review":
        raise ValueError("Asset identity review must be completed before mapping approval.")
    if connection.configuration_status != "configured" or not connection.enabled or not connection.credential_reference:
        raise ValueError("Provider connection must be configured and enabled before mapping approval.")
    if not mapping.external_id.strip():
        raise ValueError("Provider external ID is required.")
    claimed = session.scalar(
        select(AssetProviderMapping)
        .where(
            AssetProviderMapping.provider_connection_id == mapping.provider_connection_id,
            AssetProviderMapping.resource_kind == mapping.resource_kind,
            AssetProviderMapping.normalized_external_id == mapping.normalized_external_id,
            AssetProviderMapping.mapping_status == "active",
            AssetProviderMapping.valid_to.is_(None),
            AssetProviderMapping.id != mapping.id,
        )
        .with_for_update()
    )
    if claimed is not None:
        raise ValueError("Provider plant is already actively mapped to another asset.")
    quarantined = session.scalar(
        select(LegacyImportRecord.id).where(
            LegacyImportRecord.target_asset_id == asset.id,
            LegacyImportRecord.outcome == "quarantined",
        )
    )
    if quarantined is not None:
        raise ValueError("Asset is associated with quarantined legacy identity evidence.")
    mapping.mapping_status = "active"
    mapping.updated_at = utc_now()
    record_operator_action(
        session,
        actor_username=actor_username,
        action="mapping_approved",
        entity_type="asset_provider_mapping",
        entity_id=mapping.id,
        metadata={
            "mapping_id": mapping.id,
            "asset_id": mapping.asset_id,
            "connection_id": mapping.provider_connection_id,
            "provider_code": connection.provider_code,
        },
    )
    session.flush()
    return mapping


def reject_mapping(session: Session, *, mapping_id: int, actor_username: str) -> AssetProviderMapping:
    mapping = session.execute(
        select(AssetProviderMapping).where(AssetProviderMapping.id == mapping_id).with_for_update()
    ).scalar_one_or_none()
    if mapping is None:
        raise ValueError("Unknown provider mapping.")
    if mapping.mapping_status == "active":
        raise ValueError("Active mappings require an explicit replacement workflow.")
    if mapping.mapping_status == "invalid":
        return mapping
    if mapping.mapping_status != "pending_review":
        raise ValueError("Only pending mappings can be rejected.")
    mapping.mapping_status = "invalid"
    mapping.updated_at = utc_now()
    record_operator_action(
        session,
        actor_username=actor_username,
        action="mapping_rejected",
        entity_type="asset_provider_mapping",
        entity_id=mapping.id,
        metadata={"mapping_id": mapping.id, "asset_id": mapping.asset_id, "connection_id": mapping.provider_connection_id},
    )
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


def cross_connection_conflicts(session: Session, *, mapping_id: int) -> list[AssetProviderMapping]:
    """Surface, but never merge, provider IDs that collide across accounts."""
    repository = ProviderRepository(session)
    mapping = repository.mapping(mapping_id)
    if mapping is None:
        raise ValueError("Unknown provider mapping.")
    return repository.cross_connection_claims(mapping)
