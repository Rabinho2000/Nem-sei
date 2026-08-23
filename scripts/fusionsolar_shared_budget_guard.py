#!/usr/bin/env python3
"""Combined V1+V2 daily call budget guard for the shared FusionSolar account.

The ownership lease (fusionsolar_ownership_window.py /
fusionsolar_ownership_core.py) stops V1 and V2 from calling FusionSolar
*concurrently*. It says nothing about the two of them jointly exceeding the
account's real, provider-side daily call ceiling across the course of a day
-- V1's own `production_api_queue_state.daily_call_count` only ever counts
V1's calls, and V2's `provider_request_attempts` only ever counts V2's. This
script is the missing piece: it reads both, sums them per API area, and
answers one question -- "is there room for N more calls of this kind today,
without the combined total passing what V1's own operators have already
calibrated as safe for this account?" -- failing closed whenever either side
cannot be read.

We do NOT know FusionSolar's actual server-side daily ceiling (it has never
been documented anywhere in either codebase). What we do have is V1's own
locally configured `daily_budget` per API area
(`production_kpi`, `wat_history`), calibrated by whoever operates this
account against real experience of this exact account. Reusing those
numbers as the combined ceiling is a conservative choice, not a discovery of
the provider's real limit -- if V1's own budget is itself too generous, this
guard inherits that risk. That is why every stage of the rollout keeps the
actual per-stage call count small and checks this guard immediately before
and after, rather than trusting a one-time calculation.

`state`/`current_monitoring`/discovery calls have no daily_budget configured
on either side (V1 leaves API_AREA_STATE's policy as None) -- there is no
number to sum for those, so this guard does not claim to bound them
numerically. It only bounds the two areas that actually carry a configured
daily_budget: production_kpi and wat_history (device/diagnostics history).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fusionsolar_ownership_core as core  # noqa: E402


BUDGETED_AREAS = ("production_kpi", "wat_history")
# Conservative default margin: never plan to use more than this fraction of
# the combined ceiling for calls WE are about to make, leaving headroom for
# V1's own already-scheduled jobs later the same day.
DEFAULT_SAFETY_MARGIN = 0.5


class BudgetGuardError(RuntimeError):
    pass


def parse_v1_budget_rows(rows: list[dict]) -> dict[str, dict]:
    """Shared shaping logic for V1's per-area daily counters, regardless of
    whether the rows came from a direct SQLite read (host/CLI use) or from
    the ownership broker's `GET /budget` (worker/scheduler use -- those
    containers have no filesystem access to V1's database by design; see
    docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md)."""
    today = date.today().isoformat()
    by_area: dict[str, dict] = {}
    for row in rows:
        area = row["api_area"]
        if area not in BUDGETED_AREAS:
            continue
        used = int(row["daily_call_count"] or 0) if row.get("daily_count_date") == today else 0
        by_area[area] = {
            "used": used,
            "budget": row.get("daily_budget"),
            "count_date": row.get("daily_count_date"),
        }
    return by_area


def v1_budget_state(db_path: str) -> dict[str, dict]:
    """Direct SQLite read. Only usable where V1's data directory is actually
    reachable -- the host, or the broker container. NOT worker/scheduler."""
    conn = core.connect(db_path)
    try:
        core.verify_schema(conn)
        rows = core.read_v1_daily_budget_state(conn)
    finally:
        conn.close()
    return parse_v1_budget_rows(rows)


def v2_today_call_count(psql_query_fn) -> int:
    """psql_query_fn: callable that runs a SQL string against V2's Postgres
    and returns the scalar int result. Injected so this stays testable and
    so this file has no hard SQLAlchemy/psycopg dependency of its own."""
    sql = (
        "SELECT COUNT(*) FROM provider_request_attempts pra "
        "JOIN sync_runs sr ON sr.id = pra.sync_run_id "
        "JOIN provider_connections pc ON pc.id = sr.provider_connection_id "
        "WHERE pc.provider_code = 'fusionsolar' "
        "AND pra.status IN ('succeeded','failed','rate_limited') "
        "AND pra.occurred_at::date = CURRENT_DATE"
    )
    return int(psql_query_fn(sql))


def evaluate(
    *,
    v1_db_path: str | None = None,
    v1_state: dict[str, dict] | None = None,
    v2_calls_today: int,
    planned_calls: int,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> dict:
    """Fail-closed combined-budget check.

    Since V2's own bookkeeping does not currently split by API area the way
    V1's does, v2_calls_today is treated as pressure against BOTH budgeted
    areas conservatively (worst case: all of V2's calls today were the
    scarcer wat_history area) rather than assumed to be harmlessly spread
    across areas we cannot actually distinguish from here.

    Pass either `v1_db_path` (direct SQLite read -- host/broker only) or a
    pre-fetched `v1_state` (e.g. from the broker's `GET /budget`, the only
    option available inside worker/scheduler). Exactly one is required;
    fails closed if neither resolves to real V1 state.
    """
    if v1_state is None:
        if not v1_db_path:
            return {"safe": False, "reason": "fail_closed: neither v1_state nor v1_db_path was provided"}
        try:
            v1_state = v1_budget_state(v1_db_path)
        except core.OwnershipBrokerError as exc:
            return {"safe": False, "reason": f"fail_closed: cannot read V1 budget state ({exc})"}

    if not v1_state:
        return {"safe": False, "reason": "fail_closed: V1 has no budgeted api_area rows to compare against"}

    findings = []
    safe = True
    for area in BUDGETED_AREAS:
        state = v1_state.get(area)
        if state is None or state.get("budget") is None:
            findings.append({"area": area, "safe": False, "reason": "no configured daily_budget found for this area"})
            safe = False
            continue
        budget = int(state["budget"])
        combined_used = int(state["used"]) + v2_calls_today
        combined_after_plan = combined_used + planned_calls
        ceiling = budget * safety_margin
        area_safe = combined_after_plan <= ceiling
        findings.append({
            "area": area,
            "v1_used_today": state["used"],
            "v2_calls_today_conservatively_charged": v2_calls_today,
            "planned_calls": planned_calls,
            "combined_after_plan": combined_after_plan,
            "v1_daily_budget": budget,
            "safety_ceiling": ceiling,
            "safe": area_safe,
        })
        safe = safe and area_safe

    return {"safe": safe, "findings": findings, "v1_state": v1_state}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1-db", default="/opt/server/apps/Nem-sei/data/monitoring_board.db")
    parser.add_argument("--v2-calls-today", type=int, required=True, help="Count from provider_request_attempts for today, computed by the caller")
    parser.add_argument("--planned-calls", type=int, required=True, help="How many more calls the next rollout stage intends to make")
    parser.add_argument("--safety-margin", type=float, default=DEFAULT_SAFETY_MARGIN)
    args = parser.parse_args()

    result = evaluate(
        v1_db_path=args.v1_db,
        v2_calls_today=args.v2_calls_today,
        planned_calls=args.planned_calls,
        safety_margin=args.safety_margin,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
