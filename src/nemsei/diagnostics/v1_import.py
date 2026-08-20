"""Import V1's device_realtime_snapshots as device status history.

Read strictly read-only. Writes nothing to V1, ever.

`device_realtime_snapshots.provider_device_id` is V1's own `provider_devices.id`,
the exact key the M1 identity import already resolved to a V2 `devices.id` in
`legacy_import_records` (`legacy_table='provider_devices'`). No new identity
resolution happens here — a row whose device was never imported is skipped and
counted, never guessed at.

`availability_status` is imported verbatim from V1's own column: V1 already
computed it with the classification this session ported into `rules.py`, so
this module does not re-derive it, only carries it across.

`communication_status` is not imported. It reads `"recent"` on every one of
V1's 51 289 rows and carries no information; "última comunicação" is answered
by `observed_at` itself instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Device
from nemsei.assets.v1_import import open_v1_readonly
from nemsei.diagnostics.models import AVAILABILITY_STATES
from nemsei.diagnostics.service import record_device_status
from nemsei.providers.models import LegacyImportRecord


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    # A negative power or energy reading is a parse artefact, not evidence of
    # anything a device could physically do; kept out rather than imported as
    # a number a diagnostic screen would otherwise have to explain.
    return parsed if parsed >= 0 else None


@dataclass
class DeviceStatusImportManifest:
    rows_read: int = 0
    facts_created: int = 0
    facts_unchanged: int = 0
    rows_without_device: int = 0
    rows_with_unrecognised_status: int = 0
    devices_with_no_v1_history: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "facts_created": self.facts_created,
            "facts_unchanged": self.facts_unchanged,
            "rows_without_device": self.rows_without_device,
            "rows_with_unrecognised_status": self.rows_with_unrecognised_status,
        }


def _device_ids_by_legacy_provider_device_id(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(LegacyImportRecord.legacy_id, LegacyImportRecord.target_device_id).where(
            LegacyImportRecord.legacy_table == "provider_devices",
            LegacyImportRecord.target_device_id.is_not(None),
        )
    ).all()
    return {str(legacy_id): device_id for legacy_id, device_id in rows}


def import_v1_device_status(
    session: Session,
    source_path: Path,
    *,
    since: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import V1's device_realtime_snapshots as append-only device status facts."""
    manifest = DeviceStatusImportManifest()
    legacy_to_v2 = _device_ids_by_legacy_provider_device_id(session)
    connection = open_v1_readonly(source_path)
    try:
        query = "SELECT * FROM device_realtime_snapshots"
        parameters: list[Any] = []
        if since is not None:
            query += " WHERE collected_at >= ?"
            parameters.append(since)
        query += " ORDER BY provider_device_id, collected_at"
        for row in connection.execute(query, parameters):
            manifest.rows_read += 1
            device_id = legacy_to_v2.get(str(row["provider_device_id"]))
            if device_id is None:
                manifest.rows_without_device += 1
                continue
            # The canonical device's own asset_id, not V1's raw asset_id column:
            # M1's device import already resolved device-to-asset ownership, and
            # a second, independent resolution here could silently disagree with it.
            device = session.get(Device, device_id)
            if device is None:  # pragma: no cover - the FK from legacy_import_records prevents this
                manifest.rows_without_device += 1
                continue
            status = str(row["availability_status"] or "unknown").strip() or "unknown"
            if status not in AVAILABILITY_STATES:
                manifest.rows_with_unrecognised_status += 1
                status = "unknown"
            try:
                observed_at = datetime.fromisoformat(str(row["collected_at"]))
            except ValueError:
                continue
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)

            if dry_run:
                manifest.facts_created += 1
                continue
            _fact, created = record_device_status(
                session,
                device_id=device_id,
                asset_id=device.asset_id,
                source_fact_key=f"v1-device-realtime:{row['id']}",
                observed_at=observed_at,
                availability_status=status,
                active_power_kw=_decimal(row["active_power_kw"]),
                day_energy_kwh=_decimal(row["day_energy_kwh"]),
                source_kind="v1_import",
                metadata={
                    "origin": "v1_device_realtime_snapshots",
                    "v1_row_id": row["id"],
                    "v1_provider": row["provider"],
                    "v1_station_code": row["station_code"],
                    "v1_inverter_state": row["inverter_state"],
                },
            )
            if created:
                manifest.facts_created += 1
            else:
                manifest.facts_unchanged += 1
        if not dry_run:
            session.flush()
    finally:
        connection.close()
    return manifest.as_dict()
