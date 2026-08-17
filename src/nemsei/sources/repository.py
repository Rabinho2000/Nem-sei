"""Persistence-only temporal source-policy lookup."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.sources.models import AssetSourcePolicy


class SourcePolicyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active_for(self, *, asset_id: int, source_use: str, on_date: date) -> list[AssetSourcePolicy]:
        return list(self.session.scalars(select(AssetSourcePolicy).where(AssetSourcePolicy.asset_id == asset_id, AssetSourcePolicy.source_use == source_use, AssetSourcePolicy.valid_from <= on_date, (AssetSourcePolicy.valid_to.is_(None)) | (AssetSourcePolicy.valid_to >= on_date)).order_by(AssetSourcePolicy.is_fallback.asc(), AssetSourcePolicy.priority.asc(), AssetSourcePolicy.id.asc())))
