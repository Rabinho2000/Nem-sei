#!/usr/bin/env python3
"""Backfill contractual (WAT) availability from FusionSolar device history.

Two phases, deliberately separable:

1. **Ingest** closed-day history into `device_status_facts`
   (`source_kind='history_read'`) -- this is the only phase that calls the
   provider, and it goes through the same request controller, quota state,
   407 cooldown and ownership lease as every other FusionSolar read.
2. **Materialize** contractual daily availability from those facts -- pure
   database work, zero API calls, safe to re-run at any time.

`--materialize-only` runs phase 2 alone, which is what you want after
changing the calculation or repairing a day whose facts are already stored.

Resumable and idempotent by construction: a day whose facts are already
present is skipped without an API call, and re-ingesting an unchanged day
writes no new fact (`record_device_status` returns the existing row). A day
the provider has since corrected mints a **revision** superseding the old
value rather than overwriting it.

Call cost: one `getDevList` per 100 stations, plus one history call per 10
inverters, per day. Measured against the real fleet shape (130 plants / 319
inverters on V1's own contractual day) that is ~35 calls per day of history.

    docker exec nemsei-v2-worker-1 python /app/scripts/v2_availability_contractual_backfill.py \
        --connection-id 3 --from 2026-08-01 --to 2026-08-31
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

from nemsei.config import Settings
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.availability_service import (
    assets_missing_history_for_date,
    materialize_contractual_window,
)
from nemsei.integrations.fusionsolar.device_history import (
    FusionSolarDeviceHistoryService,
    history_timezone_for,
)
from nemsei.providers.repository import ProviderRepository


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--connection-id", type=int, required=True)
    parser.add_argument("--from", dest="from_date", type=_date, required=True)
    parser.add_argument("--to", dest="to_date", type=_date, required=True)
    parser.add_argument("--asset-id", type=int, action="append", dest="asset_ids",
                        help="Restrict materialization to this asset; repeatable.")
    parser.add_argument("--materialize-only", action="store_true",
                        help="Skip ingestion; recompute from already-stored facts. Zero API calls.")
    parser.add_argument("--max-api-calls", type=int, default=0,
                        help="Stop ingesting once this many provider calls have been spent (0 = no cap).")
    args = parser.parse_args()
    if args.to_date < args.from_date:
        parser.error("--to must not be before --from")

    settings = Settings.from_environment()
    sessions = build_session_factory(build_engine(settings))
    with sessions() as session:
        connection = ProviderRepository(session).connection(args.connection_id)
        if connection is None:
            parser.error("unknown connection id")
        session.expunge(connection)
    tz = history_timezone_for(connection)
    today = datetime.now(tz).date()

    synced: list[str] = []
    skipped: list[str] = []
    refused: list[str] = []
    failures: list[dict] = []
    api_calls = 0

    if not args.materialize_only:
        service = FusionSolarDeviceHistoryService(sessions, settings)
        current = args.from_date
        while current <= args.to_date:
            if current >= today:
                # A day still in progress has no contractual figure yet.
                refused.append(current.isoformat())
                current += timedelta(days=1)
                continue
            with sessions() as session:
                pending = assets_missing_history_for_date(session, connection_id=args.connection_id, target_date=current)
            if not pending:
                skipped.append(current.isoformat())
                current += timedelta(days=1)
                continue
            if args.max_api_calls and api_calls >= args.max_api_calls:
                break
            result = service.sync_device_history(args.connection_id, current, today=today)
            api_calls += result.api_calls
            if result.error is not None:
                failures.append({"date": current.isoformat(), "error": result.error.code.value, "message": result.error.message})
                # A rate limit or auth failure will not fix itself within this
                # loop; stop rather than burn the remaining budget on it.
                break
            synced.append(current.isoformat())
            current += timedelta(days=1)

    with sessions() as session:
        materialized = materialize_contractual_window(
            session, from_date=args.from_date, to_date=args.to_date, tz=tz, asset_ids=args.asset_ids
        )
        session.commit()

    print(json.dumps({
        "connection_id": args.connection_id,
        "timezone": str(tz),
        "from": args.from_date.isoformat(),
        "to": args.to_date.isoformat(),
        "ingested_days": synced,
        "already_present_days": skipped,
        "refused_open_days": refused,
        "failures": failures,
        "api_calls": api_calls,
        "materialized": materialized,
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
