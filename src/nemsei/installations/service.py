"""Creating, backfilling and reading `Installation` rows.

The backfill is the one function here that matters most: it is what makes
this migration incremental rather than a rewrite. It never touches a fact
table, never touches a provider mapping, and is safe to run twice -- an Asset
that already has an `installation_id` is left alone.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.config import Settings
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.installations.models import Installation
from nemsei.shared.clock import utc_now


@dataclass
class BackfillSummary:
    considered: int = 0
    created: int = 0
    already_linked: int = 0
    # An Asset whose (always-NULL-today) legacy `latitude`/`longitude` were
    # somehow populated. Recorded rather than silently copied, because those
    # two columns carry no provenance and this backfill must not invent one.
    legacy_coordinates_skipped: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "created": self.created,
            "already_linked": self.already_linked,
            "legacy_coordinates_skipped": self.legacy_coordinates_skipped,
        }


def backfill_installations_from_assets(session: Session) -> dict[str, Any]:
    """One `Installation` per `Asset` that does not already have one.

    Carries over the fields that are genuinely about the site --
    organization, address, locality, country, timezone -- and nothing that
    is evidence about the technical plant. Idempotent: safe to run after
    every new Asset is created, not just once at migration time.
    """
    summary = BackfillSummary()
    now = utc_now()
    # Every Asset, not a WHERE-filtered subset: `already_linked` has to be a
    # real count of what this run skipped, not a number that can only ever be
    # zero because the query already discarded the rows it would describe.
    assets = session.scalars(select(Asset).order_by(Asset.id)).all()
    for asset in assets:
        summary.considered += 1
        if asset.installation_id is not None:
            summary.already_linked += 1
            continue
        if asset.latitude is not None or asset.longitude is not None:
            summary.legacy_coordinates_skipped.append(asset.id)
        installation = Installation(
            organization_id=asset.owner_id,
            display_name=asset.canonical_name,
            country_code=asset.country_code,
            locality=asset.locality,
            address=asset.address,
            timezone=asset.timezone,
            timezone_source=asset.timezone_source,
            created_at=now,
            updated_at=now,
        )
        session.add(installation)
        session.flush()
        asset.installation_id = installation.id
        asset.updated_at = now
        summary.created += 1
    return summary.as_dict()


def installation_for_asset(session: Session, *, asset_id: int) -> Installation | None:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.installation_id is None:
        return None
    return session.get(Installation, asset.installation_id)


def coordinates_for_asset(session: Session, *, asset_id: int) -> tuple[Any, Any]:
    """`(latitude, longitude)` for one Asset's Installation, or `(None, None)`.

    The one call `monitoring.production_window` needs: it takes coordinates
    as plain arguments and does not know or care which table owns them.
    """
    installation = installation_for_asset(session, asset_id=asset_id)
    if installation is None:
        return None, None
    return installation.latitude, installation.longitude


def main() -> None:
    """Run the backfill against the database `Settings` resolves.

    No `--dry-run`: the function itself is side-effect-free to inspect
    (`considered`/`already_linked` tell you what a run would do), and unlike
    the V1 importers this makes no identity decisions worth previewing --
    every Asset without an Installation gets exactly one, deterministically.
    Wrapped in one transaction; a failure partway through leaves no Asset
    half-linked.
    """
    argparse.ArgumentParser(description="Backfill one Installation per Asset that lacks one.").parse_args()
    settings = Settings.from_environment().validate()
    factory = build_session_factory(build_engine(settings))
    with factory() as session, session.begin():
        summary = backfill_installations_from_assets(session)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
