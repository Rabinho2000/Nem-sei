"""Persistence-only device status fact lookups."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.diagnostics.models import DeviceStatusFact


class DeviceStatusRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest_fact(self, *, device_id: int, source_key: str) -> DeviceStatusFact | None:
        return self.session.scalar(
            select(DeviceStatusFact)
            .where(DeviceStatusFact.device_id == device_id, DeviceStatusFact.source_fact_key == source_key)
            .order_by(DeviceStatusFact.source_revision.desc())
        )

    def current_status_for_asset(self, *, asset_id: int) -> list[DeviceStatusFact]:
        """The most recent reading of each device belonging to this asset.

        `DISTINCT ON (device_id)` ordered by `observed_at DESC` answers "what is
        this device doing right now" from the newest observation, regardless of
        which source_fact_key it arrived under — unlike the current revision of
        one fact key, this crosses keys, because a device's most recent reading
        might be a different snapshot than its most recently *corrected* one.

        This does not exclude a superseded revision whose `observed_at` happens
        to be later than the row that replaced it, which cannot happen from a
        single straight import (every source_fact_key here is source_revision 1
        today) but could once a live read starts correcting past readings.
        Revisit if `source_revision` ever exceeds 1 in this table.
        """
        statement = (
            select(DeviceStatusFact)
            .where(DeviceStatusFact.asset_id == asset_id)
            .distinct(DeviceStatusFact.device_id)
            .order_by(DeviceStatusFact.device_id, DeviceStatusFact.observed_at.desc(), DeviceStatusFact.id.desc())
        )
        return list(self.session.scalars(statement).all())

    def history_for_device(
        self, *, device_id: int, since: datetime | None = None, limit: int = 500
    ) -> list[DeviceStatusFact]:
        statement = select(DeviceStatusFact).where(DeviceStatusFact.device_id == device_id)
        if since is not None:
            statement = statement.where(DeviceStatusFact.observed_at >= since)
        return list(
            self.session.scalars(statement.order_by(DeviceStatusFact.observed_at.desc()).limit(limit)).all()
        )
