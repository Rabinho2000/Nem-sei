"""Persistence operations for provider connections and plant mappings."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.providers.models import AssetProviderMapping, ProviderConnection


class ProviderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def connection(self, connection_id: int) -> ProviderConnection | None:
        return self.session.get(ProviderConnection, connection_id)

    def mapping(self, mapping_id: int) -> AssetProviderMapping | None:
        return self.session.get(AssetProviderMapping, mapping_id)

    def active_external_claim(
        self, *, connection_id: int, normalized_external_id: str
    ) -> AssetProviderMapping | None:
        return self.session.scalar(
            select(AssetProviderMapping).where(
                AssetProviderMapping.provider_connection_id == connection_id,
                AssetProviderMapping.resource_kind == "plant",
                AssetProviderMapping.normalized_external_id == normalized_external_id,
                AssetProviderMapping.mapping_status == "active",
                AssetProviderMapping.valid_to.is_(None),
            )
        )

    def mappings_for_asset(self, asset_id: int) -> list[AssetProviderMapping]:
        return list(
            self.session.scalars(
                select(AssetProviderMapping)
                .where(AssetProviderMapping.asset_id == asset_id)
                .order_by(AssetProviderMapping.valid_from.desc(), AssetProviderMapping.id.desc())
            )
        )

    def add(self, entity: object) -> None:
        self.session.add(entity)
