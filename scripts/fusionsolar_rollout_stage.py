#!/usr/bin/env python3
"""One staged FusionSolar rollout step: acquire ownership, activate N more
installations onto the live connection, sync them, release, report.

Meant to be run *inside* a V2 container (worker/web), where the `nemsei`
package and its Postgres session factory are importable, e.g.:

    docker exec nemsei-v2-worker-1 python /app/scripts/fusionsolar_rollout_stage.py \
        --target-active 5 --planned-calls 20

Every stage:
  1. Reads V1's live budget state + V2's own call count today and refuses to
     proceed if the combined total would not stay under the safety margin
     (fusionsolar_shared_budget_guard.py). Fails closed on any read error.
  2. Acquires V1's account lease through the ownership broker
     (NEMSEI_V1_OWNERSHIP_BROKER_URL) for the duration of this stage. This is
     a courtesy/observability wrapper for the *batch*; the actual mandatory
     enforcement lives in request_control.py + v1_ownership.py and applies to
     every individual HTTP call regardless of whether this script is used.
  3. Sets `assets.timezone` for any FusionSolar-legacy-mapped asset that does
     not have one yet, restricted to the mainland-Portugal evidence documented
     in docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md's rollout write-up. Idempotent.
  4. Picks the next `target_active - current_active` pending_review legacy
     mappings (status_detail ACTIVE only, ordered by asset id for
     reproducibility) and, for each: creates a live `active` mapping +
     monitoring/production source policies via
     nemsei.providers.service.create_mapping /
     nemsei.sources.service.create_source_policy -- the same deterministic,
     no-fuzzy-matching path already used for the two mappings activated
     before this rollout (external_id copied verbatim from the legacy
     mapping; no invented identity).
  5. Runs FusionSolarMonitoringService.sync_current_monitoring and
     FusionSolarProductionService.sync_incremental for the connection once,
     reports expected/received/accepted/rejected/errors per run.
  6. Releases the ownership window and confirms handback.

A single asset's own data/mapping problem does not block the batch: it is
left at pending_review and reported by name/reason. The whole stage blocks
(nothing new activated) only on: budget guard failure, ownership acquire
failure, or a sync result that fails for a reason other than a per-asset
selection finding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
from fusionsolar_shared_budget_guard import evaluate as evaluate_budget, parse_v1_budget_rows  # noqa: E402

from sqlalchemy import bindparam, select, text

from nemsei.config import Settings, read_secret_value
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.integrations.fusionsolar.monitoring import FusionSolarMonitoringService
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.integrations.fusionsolar.session_cache import default_session_cache
from nemsei.providers.models import ProviderConnection
from nemsei.providers.service import create_mapping
from nemsei.sources.service import create_source_policy

LIVE_CONNECTION_KEY = "fusion-canary"
LEGACY_CONNECTION_KEY = "v1-fusionsolar-legacy"
ACTOR = "nemsei-v2-rollout-bot"

# V2's imported `assets.lifecycle_status` is 'unknown' for all 134
# FusionSolar-legacy-mapped assets -- V1's own `status_detail`
# (ACTIVE/CANCELLED/ON HOLD/blank) was never carried into that column, and
# `legacy_import_records.evidence_json` for these rows is empty too. Cross-
# checked by hand against V1's live database on 2026-08-23 (host-side read,
# never through worker/scheduler, which have no V1 access): these V2 asset
# ids are the ones whose V1 status_detail was NOT 'ACTIVE' (1 CANCELLED, 3
# ON HOLD, 6 blank/unclassified -- blank is treated the same as inactive
# here, not the same as ACTIVE, since there is no positive evidence either
# way). Do not activate these without a human confirming their real status
# first; this is a deliberate, evidenced exclusion, not a guess.
V1_NON_ACTIVE_ASSET_IDS = {25, 152, 301, 308, 320, 322, 326, 329, 330, 331}
TIMEZONE_EVIDENCE = (
    "Portfolio-wide address/postal-code/lat-long scan of all 135 V1 FusionSolar-mapped "
    "assets found zero Azores (95xx-98xx postal / -31..-24E lon) or Madeira "
    "(90xx-93xx postal / -17..-16E lon) signal; every address resolves to mainland "
    "Portugal. Matches the precedent already set for asset 180 (Entre Vinhas e Mar, "
    "Obidos -> Europe/Lisbon, M4 canary)."
)


def _account_key(connection: ProviderConnection) -> str:
    reference = (connection.credential_reference or "").strip()
    prefix = f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}"
    username = read_secret_value(value_name=f"{prefix}_USERNAME", file_name=f"{prefix}_USERNAME_FILE")
    base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip()
    identity = "|".join((connection.provider_code.strip().lower(), username.strip().lower(), base_url.strip().lower().rstrip("/"), "account"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _login_count(sessions, connection_id: int) -> int:
    with sessions() as s:
        return int(s.execute(text(
            "SELECT COUNT(*) FROM provider_request_attempts pra "
            "JOIN provider_request_states prs ON prs.id = pra.request_state_id "
            "WHERE prs.provider_connection_id = :cid AND prs.endpoint_family = 'authentication' "
            "AND pra.status = 'succeeded'"
        ), {"cid": connection_id}).scalar())


def _broker_request(method: str, path: str, body: dict | None = None) -> dict:
    url = os.environ["NEMSEI_V1_OWNERSHIP_BROKER_URL"].rstrip("/") + path
    token = read_secret_value(value_name="NEMSEI_V1_OWNERSHIP_BROKER_TOKEN", file_name="NEMSEI_V1_OWNERSHIP_BROKER_TOKEN_FILE")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-active", type=int, required=True)
    parser.add_argument("--planned-calls", type=int, required=True)
    parser.add_argument("--v1-db", default="/opt/server/apps/Nem-sei/data/monitoring_board.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_environment()
    sessions = build_session_factory(build_engine(settings))
    report: dict = {"stage_target_active": args.target_active, "dry_run": args.dry_run}

    with sessions() as s:
        v2_calls_today = int(s.execute(text(
            "SELECT COUNT(*) FROM provider_request_attempts pra "
            "JOIN sync_runs sr ON sr.id = pra.sync_run_id "
            "JOIN provider_connections pc ON pc.id = sr.provider_connection_id "
            "WHERE pc.provider_code = 'fusionsolar' "
            "AND pra.status IN ('succeeded','failed','rate_limited') "
            "AND pra.occurred_at::date = CURRENT_DATE"
        )).scalar())
    try:
        v1_budget_rows = _broker_request("GET", "/budget")["v1_daily_budget_state"]
        v1_state = parse_v1_budget_rows(v1_budget_rows)
        budget = evaluate_budget(v1_state=v1_state, v2_calls_today=v2_calls_today, planned_calls=args.planned_calls)
    except Exception as exc:
        budget = {"safe": False, "reason": f"fail_closed: could not fetch V1 budget via broker ({exc})"}
    report["budget_guard"] = budget
    if not budget["safe"]:
        print(json.dumps(report, indent=2, default=str))
        print("BLOCKED: shared budget guard did not clear this stage.", file=sys.stderr)
        return 1

    with sessions() as s:
        connection = s.execute(select(ProviderConnection).where(ProviderConnection.connection_key == LIVE_CONNECTION_KEY)).scalar_one()
    account_key = _account_key(connection)
    logins_before = _login_count(sessions, connection.id)
    owner = f"nemsei-v2-rollout-stage-{int(time.time())}"

    acquired = _broker_request("POST", "/acquire", {"account_key": account_key, "owner": owner, "lease_seconds": 120})
    report["ownership_acquire"] = acquired
    if not acquired.get("granted"):
        print(json.dumps(report, indent=2, default=str))
        print("BLOCKED: could not acquire V1 ownership window for this stage.", file=sys.stderr)
        return 1

    try:
        # 3. Timezone (idempotent).
        with sessions() as s:
            if not args.dry_run:
                # No `bulk_timezone_resolved` entry in
                # OPERATOR_AUDIT_ACTIONS (providers/models.py) -- this event
                # type is not modeled by the app today, and record_operator_
                # action() rejects unknown actions by design. Writing it via
                # raw SQL anyway would corrupt the audit contract rather than
                # honor it, so the evidence lives in this script's own
                # printed report (and docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md)
                # instead. Worth a real OPERATOR_AUDIT_ACTIONS addition later.
                result = s.execute(text("""
                    UPDATE assets SET timezone = 'Europe/Lisbon', timezone_source = 'address_geocode_evidence', country_code = 'PT'
                    WHERE id IN (
                        SELECT a.id FROM assets a
                        JOIN asset_provider_mappings apm ON apm.asset_id = a.id
                        JOIN provider_connections pc ON pc.id = apm.provider_connection_id
                        WHERE pc.connection_key = :legacy AND apm.resource_kind = 'plant'
                        AND (a.timezone IS NULL OR a.timezone = '')
                    )
                """), {"legacy": LEGACY_CONNECTION_KEY})
                report["timezone_fixed"] = result.rowcount
                report["timezone_evidence"] = TIMEZONE_EVIDENCE
                s.commit()
            else:
                count = s.execute(text("""
                    SELECT COUNT(*) FROM assets a
                    JOIN asset_provider_mappings apm ON apm.asset_id = a.id
                    JOIN provider_connections pc ON pc.id = apm.provider_connection_id
                    WHERE pc.connection_key = :legacy AND apm.resource_kind = 'plant'
                    AND (a.timezone IS NULL OR a.timezone = '')
                """), {"legacy": LEGACY_CONNECTION_KEY}).scalar()
                report["timezone_would_fix"] = count

        # 4. Pick candidates and activate up to target_active.
        with sessions() as s:
            current_active = s.execute(text("""
                SELECT COUNT(*) FROM asset_provider_mappings apm
                JOIN provider_connections pc ON pc.id = apm.provider_connection_id
                WHERE pc.connection_key = :live AND apm.resource_kind = 'plant' AND apm.mapping_status = 'active'
            """), {"live": LIVE_CONNECTION_KEY}).scalar()
            report["active_before"] = current_active
            need = max(0, args.target_active - current_active)
            report["need_to_activate"] = need

            candidates = s.execute(text("""
                SELECT apm.id AS legacy_mapping_id, apm.asset_id, apm.external_id, apm.external_name,
                       a.canonical_name, a.timezone
                FROM asset_provider_mappings apm
                JOIN provider_connections pc ON pc.id = apm.provider_connection_id
                JOIN assets a ON a.id = apm.asset_id
                WHERE pc.connection_key = :legacy AND apm.resource_kind = 'plant' AND apm.mapping_status = 'pending_review'
                AND apm.asset_id NOT IN :excluded_ids
                AND NOT EXISTS (
                    SELECT 1 FROM asset_provider_mappings live_apm
                    JOIN provider_connections live_pc ON live_pc.id = live_apm.provider_connection_id
                    WHERE live_pc.connection_key = :live AND live_apm.asset_id = apm.asset_id
                      AND live_apm.resource_kind = 'plant' AND live_apm.mapping_status = 'active'
                )
                ORDER BY apm.asset_id
                LIMIT :need
            """).bindparams(bindparam("excluded_ids", expanding=True)), {
                "legacy": LEGACY_CONNECTION_KEY, "live": LIVE_CONNECTION_KEY, "need": need,
                "excluded_ids": sorted(V1_NON_ACTIVE_ASSET_IDS) or [-1],
            }).mappings().all()

        report["known_exclusions_not_offered"] = sorted(V1_NON_ACTIVE_ASSET_IDS)
        report["activated"] = []
        report["blocked"] = []
        for row in candidates:
            if not row["timezone"]:
                # Should not happen -- step 3 just set it -- but never
                # activate on an assumed timezone; fail this one asset
                # closed rather than silently proceed.
                report["blocked"].append({"asset_id": row["asset_id"], "canonical_name": row["canonical_name"], "reason": "no resolved timezone"})
                continue
            if args.dry_run:
                report["activated"].append({"asset_id": row["asset_id"], "canonical_name": row["canonical_name"], "dry_run": True})
                continue
            with sessions() as s:
                try:
                    mapping = create_mapping(
                        s,
                        asset_id=row["asset_id"],
                        provider_connection_id=connection.id,
                        external_id=row["external_id"],
                        external_name=row["external_name"],
                        resource_kind="plant",
                        mapping_status="active",
                        notes=f"Rollout-activated {date.today().isoformat()} from legacy mapping {row['legacy_mapping_id']}.",
                    )
                    for use in ("monitoring", "production"):
                        create_source_policy(
                            s,
                            asset_id=row["asset_id"],
                            provider_mapping_id=mapping.id,
                            source_use=use,
                            priority=1,
                            valid_from=date.today(),
                            actor_username=ACTOR,
                        )
                    s.commit()
                    report["activated"].append({"asset_id": row["asset_id"], "canonical_name": row["canonical_name"], "mapping_id": mapping.id})
                except ValueError as exc:
                    s.rollback()
                    report["blocked"].append({"asset_id": row["asset_id"], "canonical_name": row["canonical_name"], "reason": str(exc)})

        # 5. Run the real portfolio-wide syncs once, if not dry-run.
        if not args.dry_run:
            cache = default_session_cache()
            monitoring = FusionSolarMonitoringService(sessions, settings, session_cache=cache).sync_current_monitoring(connection.id)
            production = FusionSolarProductionService(sessions, settings, session_cache=cache).sync_incremental(connection.id, start_date=date.today(), end_date=date.today())
            report["logins_this_stage"] = _login_count(sessions, connection.id) - logins_before
            report["monitoring_result"] = monitoring.__dict__ if hasattr(monitoring, "__dict__") else str(monitoring)
            report["production_result"] = production.__dict__ if hasattr(production, "__dict__") else str(production)
    finally:
        released = _broker_request("POST", "/release", {"account_key": account_key, "owner": owner})
        report["ownership_release"] = released

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
