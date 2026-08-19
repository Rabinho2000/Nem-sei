"""Persistence operations for provider connections and plant mappings."""
from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select
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
        self, *, connection_id: int, normalized_external_id: str, resource_kind: str = "plant"
    ) -> AssetProviderMapping | None:
        return self.session.scalar(
            select(AssetProviderMapping).where(
                AssetProviderMapping.provider_connection_id == connection_id,
                AssetProviderMapping.resource_kind == resource_kind,
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

    def current_mappings_for_connection(self, connection_id: int) -> list[AssetProviderMapping]:
        """Return only unresolved/current plant mappings for live reconciliation."""
        return list(
            self.session.scalars(
                select(AssetProviderMapping)
                .where(
                    AssetProviderMapping.provider_connection_id == connection_id,
                    AssetProviderMapping.resource_kind == "plant",
                    AssetProviderMapping.valid_to.is_(None),
                    AssetProviderMapping.mapping_status.in_(("active", "invalid", "pending_review")),
                )
                .order_by(AssetProviderMapping.id)
            )
        )

    def mappings_for_connection_on_date(self, connection_id: int, on_date: date) -> list[AssetProviderMapping]:
        """Plant mappings effective for a historical source period."""
        return list(
            self.session.scalars(
                select(AssetProviderMapping)
                .where(
                    AssetProviderMapping.provider_connection_id == connection_id,
                    AssetProviderMapping.resource_kind == "plant",
                    AssetProviderMapping.valid_from <= on_date,
                    or_(AssetProviderMapping.valid_to.is_(None), AssetProviderMapping.valid_to >= on_date),
                )
                .order_by(AssetProviderMapping.id)
            )
        )

    def cross_connection_claims(self, mapping: AssetProviderMapping) -> list[AssetProviderMapping]:
        return list(
            self.session.scalars(
                select(AssetProviderMapping)
                .join(ProviderConnection)
                .where(
                    ProviderConnection.provider_code
                    == self.session.get(ProviderConnection, mapping.provider_connection_id).provider_code,
                    AssetProviderMapping.provider_connection_id != mapping.provider_connection_id,
                    AssetProviderMapping.resource_kind == mapping.resource_kind,
                    AssetProviderMapping.normalized_external_id == mapping.normalized_external_id,
                    AssetProviderMapping.mapping_status == "active",
                    AssetProviderMapping.valid_to.is_(None),
                )
            )
        )

    def list_connections(self) -> list[ProviderConnection]:
        return list(self.session.scalars(select(ProviderConnection).order_by(ProviderConnection.provider_code, ProviderConnection.display_name)))

    def add(self, entity: object) -> None:
        self.session.add(entity)
