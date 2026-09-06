#!/usr/bin/env python3
"""Materialize device/asset daily availability from already-persisted facts.

Reads nothing but `device_status_facts`, `asset_provider_mappings` and
`devices` -- makes **zero** FusionSolar API calls (see
`diagnostics/availability_service.py`'s own docstring for why that is
structurally true, not just a claim). Safe to re-run: each day is
idempotently recomputed (delete+insert), never appended to.

Meant to run *inside* a V2 container, where the `nemsei` package and its
Postgres session factory are importable, e.g.:

    docker exec nemsei-v2-worker-1 python /app/scripts/v2_availability_materialize.py \
        --asset-id 153 --from 2026-08-25 --to 2026-09-03

Omitting `--asset-id` covers every FusionSolar-mapped asset for the range,
which is what a historical backfill normally wants. Because the whole
window is one batched pass, this stays a bounded number of queries rather
than one per asset per day.

See `docs/v2/AVAILABILITY_MIGRATION_PLAN.md` §3/§11.
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from nemsei.config import Settings
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.availability_service import materialize_availability_window


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset-id",
        type=int,
        action="append",
        dest="asset_ids",
        help="Restrict to this asset; repeatable. Omit to cover every FusionSolar-mapped asset.",
    )
    parser.add_argument("--from", dest="from_date", type=_date, required=True)
    parser.add_argument("--to", dest="to_date", type=_date, required=True)
    args = parser.parse_args()

    if args.to_date < args.from_date:
        parser.error("--to must not be before --from")

    settings = Settings.from_environment()
    sessions = build_session_factory(build_engine(settings))
    with sessions() as session:
        # One batched pass over the whole (assets x days) window: a bounded
        # number of queries regardless of how many asset-days it covers, so a
        # fleet-wide backfill is not hundreds of thousands of round trips.
        summary = materialize_availability_window(
            session, from_date=args.from_date, to_date=args.to_date, asset_ids=args.asset_ids
        )
        session.commit()

    print(
        json.dumps(
            {
                "asset_ids": args.asset_ids or "all_fusionsolar_mapped",
                "from": args.from_date.isoformat(),
                "to": args.to_date.isoformat(),
                **summary,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
