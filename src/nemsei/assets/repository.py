"""Persistence operations for the asset domain only."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, AssetAlias, Device, Organization


class AssetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def organization(self, organization_id: int) -> Organization | None:
        return self.session.get(Organization, organization_id)

    def asset(self, asset_id: int) -> Asset | None:
        return self.session.get(Asset, asset_id)

    def list_assets(self) -> list[Asset]:
        return list(self.session.scalars(select(Asset).order_by(Asset.canonical_name, Asset.id)))

    def list_organizations(self) -> list[Organization]:
        return list(self.session.scalars(select(Organization).order_by(Organization.display_name, Organization.id)))

    def alias(self, alias_id: int) -> AssetAlias | None:
        return self.session.get(AssetAlias, alias_id)

    def device(self, device_id: int) -> Device | None:
        return self.session.get(Device, device_id)

    def list_devices(self, asset_id: int) -> list[Device]:
        return list(
            self.session.scalars(
                select(Device).where(Device.asset_id == asset_id).order_by(Device.label, Device.id)
            )
        )

    def active_serial_claim(self, *, asset_id: int, normalized_serial_number: str) -> Device | None:
        return self.session.scalar(
            select(Device).where(
                Device.asset_id == asset_id,
                Device.normalized_serial_number == normalized_serial_number,
                Device.valid_to.is_(None),
            )
        )

    def add(self, entity: object) -> None:
        self.session.add(entity)
