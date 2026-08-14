"""Persistence operations for the asset domain only."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, AssetAlias, Organization


class AssetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def organization(self, organization_id: int) -> Organization | None:
        return self.session.get(Organization, organization_id)

    def asset(self, asset_id: int) -> Asset | None:
        return self.session.get(Asset, asset_id)

    def list_assets(self) -> list[Asset]:
        return list(self.session.scalars(select(Asset).order_by(Asset.canonical_name, Asset.id)))

    def alias(self, alias_id: int) -> AssetAlias | None:
        return self.session.get(AssetAlias, alias_id)

    def add(self, entity: object) -> None:
        self.session.add(entity)
