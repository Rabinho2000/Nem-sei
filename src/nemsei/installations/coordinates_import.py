"""Bring V1's plant coordinates across, with the provenance V1 recorded.

Deliberately separate from `assets/v1_import.py` rather than a column added
to its `SELECT`. That importer fingerprints each source row with
`row_hash(row, row.keys())` and compares the fingerprint to decide `reused`
against `changed_source`; widening its query would change every one of the
267 fingerprints at once and report the whole parque as changed at the
source, destroying the very signal the mechanism exists to give.

Target is `Installation`, not `Asset` -- coordinates are a property of the
physical site, not the technical plant, and `Asset.latitude`/`.longitude` are
kept only for backward compatibility (see `assets/models.py`). This means the
backfill (`installations/service.py::backfill_installations_from_assets`)
must run before this: an Asset with no `installation_id` yet has nowhere to
put a coordinate, and is reported as `unlinked` rather than silently skipped.

What this does is narrow and idempotent: for installations that already exist
in V2 and have no coordinates, copy V1's pair and the two provenance fields
alongside it. It never overwrites a value already present, because the only
way a V2 installation can have one today is that a person put it there, and a
bulk import must not outrank a person.

The link between a V1 asset id and a V2 installation is the identity import's
own record -- `legacy_import_records.target_asset_id`, followed through
`Asset.installation_id` -- never a name match.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.assets.v1_import import LegacyImportError, open_v1_readonly, source_sha256
from nemsei.config import Settings
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.installations.models import COORDINATE_CONFIDENCES, COORDINATE_SOURCES, Installation
from nemsei.providers.models import LegacyImportRecord
from nemsei.shared.clock import utc_now


# Anything outside this is not a coordinate, it is a parsing accident: a
# swapped pair, a decimal comma read as a thousands separator, or the (0, 0)
# that geocoders return when they fail. Refusing them here keeps a nonsense
# value from silently producing a confident sunrise on the wrong continent.
LATITUDE_BOUNDS = (Decimal("-90"), Decimal("90"))
LONGITUDE_BOUNDS = (Decimal("-180"), Decimal("180"))
NULL_ISLAND_RADIUS = Decimal("0.01")


@dataclass
class CoordinateImportSummary:
    """What the import did, per installation, with nothing folded away."""

    source_database_sha256: str
    dry_run: bool
    considered: int = 0
    imported: int = 0
    already_present: int = 0
    absent_in_v1: int = 0
    unlinked: int = 0
    # An Asset that exists but has no Installation yet -- the backfill has
    # not run. Kept apart from `unlinked` (a V1 row with no V2 counterpart at
    # all), because the fix for this one is "run the backfill", not "resolve
    # an import conflict".
    no_installation: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    by_confidence: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_database_sha256": self.source_database_sha256,
            "dry_run": self.dry_run,
            "considered": self.considered,
            "imported": self.imported,
            "already_present": self.already_present,
            "absent_in_v1": self.absent_in_v1,
            "unlinked": self.unlinked,
            "no_installation": self.no_installation,
            "rejected": self.rejected,
            "by_confidence": dict(sorted(self.by_confidence.items())),
        }


def parse_coordinate(value: Any, *, bounds: tuple[Decimal, Decimal]) -> Decimal | None:
    """A V1 coordinate as a bounded Decimal, or `None` if it is not one."""
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or not bounds[0] <= parsed <= bounds[1]:
        return None
    # The column is Numeric(10, 7); more precision than that is false anyway.
    return parsed.quantize(Decimal("0.0000001"))


def _v1_coordinates(source_db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = source_db.execute(
        "SELECT id, project_name, latitude, longitude, coordinates_source, coordinates_confidence"
        " FROM assets WHERE latitude IS NOT NULL AND longitude IS NOT NULL ORDER BY id"
    )
    return {str(row["id"]): row for row in rows}


def _linked_assets(session: Session) -> dict[str, int]:
    """V1 asset id to V2 asset id, from the identity import's own records."""
    rows = session.execute(
        select(LegacyImportRecord.legacy_id, LegacyImportRecord.target_asset_id)
        .where(
            LegacyImportRecord.legacy_table == "assets",
            LegacyImportRecord.target_asset_id.is_not(None),
        )
        .order_by(LegacyImportRecord.id)
    ).all()
    return {str(legacy_id): int(asset_id) for legacy_id, asset_id in rows}


def import_v1_coordinates(
    session: Session, source_path: Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """Copy V1 coordinates onto V2 Installations that have none."""
    source_path = source_path.expanduser().resolve()
    bound = session.get_bind().url.database
    if bound and Path(str(bound)).resolve() == source_path:
        raise LegacyImportError("The V1 source database cannot be the V2 database.")

    summary = CoordinateImportSummary(source_database_sha256=source_sha256(source_path), dry_run=dry_run)
    source_db = open_v1_readonly(source_path)
    try:
        legacy_rows = _v1_coordinates(source_db)
        linked = _linked_assets(session)
        now = utc_now()

        for legacy_id, row in legacy_rows.items():
            summary.considered += 1
            asset_id = linked.get(legacy_id)
            if asset_id is None:
                # A V1 plant that never became a V2 installation -- a
                # quarantined duplicate, or one an operator discarded.
                summary.unlinked += 1
                continue
            asset = session.get(Asset, asset_id)
            if asset is None:
                summary.unlinked += 1
                continue
            if asset.installation_id is None:
                summary.no_installation += 1
                continue
            installation = session.get(Installation, asset.installation_id)
            if installation is None:
                summary.no_installation += 1
                continue
            if installation.latitude is not None or installation.longitude is not None:
                summary.already_present += 1
                continue

            latitude = parse_coordinate(row["latitude"], bounds=LATITUDE_BOUNDS)
            longitude = parse_coordinate(row["longitude"], bounds=LONGITUDE_BOUNDS)
            source = (row["coordinates_source"] or "").strip() or None
            confidence = (row["coordinates_confidence"] or "").strip() or None

            reason = None
            if latitude is None or longitude is None:
                reason = "coordenada fora de intervalo ou ilegível"
            elif abs(latitude) < NULL_ISLAND_RADIUS and abs(longitude) < NULL_ISLAND_RADIUS:
                # (0, 0) is in the Gulf of Guinea. It is what a failed
                # geocode returns, never where a Solcor plant is.
                reason = "coordenada (0, 0) — geocodificação falhada"
            elif source not in COORDINATE_SOURCES:
                # No provenance means no import: the CHECK constraint would
                # refuse the row anyway, and inventing a source to satisfy it
                # is the failure mode this whole column exists to stop.
                reason = f"origem desconhecida: {source!r}"
            elif confidence is not None and confidence not in COORDINATE_CONFIDENCES:
                reason = f"confiança desconhecida: {confidence!r}"

            if reason is not None:
                summary.rejected.append(
                    {
                        "legacy_id": legacy_id,
                        "project_name": row["project_name"],
                        "asset_id": asset_id,
                        "installation_id": installation.id,
                        "reason": reason,
                    }
                )
                continue

            if not dry_run:
                installation.latitude = latitude
                installation.longitude = longitude
                installation.coordinates_source = source
                installation.coordinates_confidence = confidence
                installation.updated_at = now
            summary.imported += 1
            key = confidence or "sem confiança registada"
            summary.by_confidence[key] = summary.by_confidence.get(key, 0) + 1

        # Installations V2 still cannot place, and V1 could not either. This
        # is the honest size of the remaining gap: not a failure of the
        # import, just plants nobody has ever located. Every one of them
        # answers `unknown` to the production window.
        offered = {linked[legacy_id] for legacy_id in legacy_rows if legacy_id in linked}
        located_asset_ids = {
            asset_id
            for asset_id, latitude in session.execute(
                select(Asset.id, Installation.latitude).join(Installation, Installation.id == Asset.installation_id)
            ).all()
            if latitude is not None
        }
        summary.absent_in_v1 = sum(
            1
            for asset_id in session.scalars(select(Asset.id))
            if asset_id not in offered and asset_id not in located_asset_ids
        )
        return summary.as_dict()
    finally:
        source_db.close()


def main() -> None:
    """Same shape as `nemsei.assets.v1_import`, deliberately.

    A dry run opens a real session because it reads the V2 side to decide what
    it would do -- which installations already carry a coordinate, and which
    V1 rows link to one at all. It commits nothing.
    """
    parser = argparse.ArgumentParser(description="Import V1 plant coordinates into V2 Installations.")
    parser.add_argument("--v1-db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_environment().validate()
    factory = build_session_factory(build_engine(settings))
    with factory() as session:
        if args.dry_run:
            summary = import_v1_coordinates(session, args.v1_db, dry_run=True)
            session.rollback()
        else:
            with session.begin():
                summary = import_v1_coordinates(session, args.v1_db)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
