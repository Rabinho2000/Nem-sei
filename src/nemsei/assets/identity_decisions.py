"""Operator rulings that resolve ambiguous V1 identity evidence.

The importer never merges installations by name similarity. When one normalized
V1 asset name covers several source rows, a human decides which row is the
canonical installation and which rows are legacy noise. Recording that decision
here keeps the import repeatable, replayable and auditable, instead of turning
it into a manual database edit that no later run can reproduce.
"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.config import Settings
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.providers.audit import record_operator_action
from nemsei.providers.models import IDENTITY_DECISIONS, LegacyIdentityDecision
from nemsei.shared.clock import utc_now


def record_identity_decision(
    session: Session,
    *,
    legacy_table: str,
    legacy_id: int | str,
    decision: str,
    actor_username: str,
    reason: str | None = None,
    source_database_sha256: str | None = None,
) -> LegacyIdentityDecision:
    if decision not in IDENTITY_DECISIONS:
        raise ValueError("Identity decision must be canonical or discard.")
    table = legacy_table.strip()
    identifier = str(legacy_id).strip()
    actor = actor_username.strip()
    if not table or not identifier or not actor:
        raise ValueError("Identity decisions require a legacy table, legacy ID and actor.")
    existing = session.scalar(
        select(LegacyIdentityDecision).where(
            LegacyIdentityDecision.legacy_table == table,
            LegacyIdentityDecision.legacy_id == identifier,
        )
    )
    now = utc_now()
    if existing is None:
        existing = LegacyIdentityDecision(legacy_table=table, legacy_id=identifier, decided_at=now)
        session.add(existing)
    existing.decision = decision
    existing.reason = reason.strip() if reason else None
    existing.actor_username = actor
    existing.source_database_sha256 = source_database_sha256
    existing.decided_at = now
    session.flush()
    record_operator_action(
        session,
        actor_username=actor,
        action="identity_decision_recorded",
        entity_type="legacy_identity_decision",
        entity_id=existing.id,
        metadata={"legacy_table": table, "legacy_id": identifier, "decision": decision},
    )
    return existing


def load_identity_decisions(session: Session, *, legacy_table: str) -> dict[str, str]:
    return {
        record.legacy_id: record.decision
        for record in session.scalars(
            select(LegacyIdentityDecision).where(LegacyIdentityDecision.legacy_table == legacy_table)
        )
    }


def resolve_duplicate_groups(
    groups: dict[str, list[int]], decisions: dict[str, str]
) -> dict[str, int]:
    """Return the canonical legacy ID for each fully decided duplicate group.

    A group is only resolved when exactly one member is canonical and every
    other member is explicitly discarded. Anything less stays ambiguous and the
    importer keeps quarantining it.
    """
    resolved: dict[str, int] = {}
    for name, legacy_ids in groups.items():
        canonical = [legacy_id for legacy_id in legacy_ids if decisions.get(str(legacy_id)) == "canonical"]
        discarded = [legacy_id for legacy_id in legacy_ids if decisions.get(str(legacy_id)) == "discard"]
        if len(canonical) == 1 and len(discarded) == len(legacy_ids) - 1:
            resolved[name] = canonical[0]
    return resolved


def decision_supersedes_prior_record(
    prior_outcome: str | None,
    prior_created_target: bool,
    *,
    group_resolved: bool,
    row_is_canonical: bool,
) -> bool:
    """Whether an operator decision invalidates a replayed import record.

    Replay normally short-circuits an unchanged source row as `reused`, which is
    what protects manual V2 edits. A quarantined row created nothing, so once a
    decision resolves its group that replay would silently keep the row out of
    V2 forever. Re-evaluate it, unless the prior record already reflects the
    decision.
    """
    if not group_resolved or prior_outcome is None or prior_created_target:
        return False
    if row_is_canonical:
        return True
    return prior_outcome != "excluded"


def main() -> None:
    parser = argparse.ArgumentParser(description="Record an operator identity decision for a V1 source row.")
    parser.add_argument("--legacy-table", default="assets")
    parser.add_argument("--legacy-id", required=True)
    parser.add_argument("--decision", required=True, choices=list(IDENTITY_DECISIONS))
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--source-sha256")
    args = parser.parse_args()
    settings = Settings.from_environment().validate()
    factory = build_session_factory(build_engine(settings))
    with factory() as session, session.begin():
        decision = record_identity_decision(
            session,
            legacy_table=args.legacy_table,
            legacy_id=args.legacy_id,
            decision=args.decision,
            actor_username=args.actor,
            reason=args.reason,
            source_database_sha256=args.source_sha256,
        )
        print(json.dumps({"legacy_table": decision.legacy_table, "legacy_id": decision.legacy_id, "decision": decision.decision}, sort_keys=True))


if __name__ == "__main__":
    main()
