"""Why an installation is not receiving daily production, said in one word.

The question this answers came from the fleet, not from a design: many
installations simply have no daily production and the only way to find out
why was to open psql and walk the chain by hand -- mapping, then source
policy, then connection, then the environment contract, then the cursor,
then the scheduler, then the last sync run. Seven places, in an order you
had to already know, and the answer to "why is this plant empty" was
therefore only ever available to whoever had walked it before.

So the chain is walked here, in that order, and the first link that is
broken is the answer. The order matters and is not arbitrary: a missing
source policy is not worth reporting for an asset that has no mapping at
all, and a failed sync is not the reason a plant is empty when its
connection was never scheduled. Reporting the deepest cause and only the
deepest cause is what makes the state actionable rather than a list of
everything that is also true.

**No secrets leave this module.** `credential_reference` is a *name* of a
secret, not a secret, and even that is reduced to a boolean here; the
environment values behind it (`<PREFIX>_PRODUCTION_TIMEZONE`,
`<PREFIX>_PRODUCTION_UNIT`) are reported only as present/absent, never
read back out.

**No provider call happens here, ever.** Everything is read from tables
this platform already wrote. That is a requirement, not a happy accident:
the screen this feeds exists precisely because the shared FusionSolar
account is rate-limited, and a diagnostic that spent call budget to
explain why calls are failing would be worse than no diagnostic.

**No N+1.** Every lookup is batched across the whole fleet -- one query
per fact, six queries total, regardless of how many installations there
are. A per-asset loop over `resolve_source_policy` would be 267 round
trips on a page whose whole purpose is to be opened when things are slow.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.jobs.models import ScheduleState
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCapability, ProviderCode
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sources.models import AssetSourcePolicy
from nemsei.sync.models import SyncRun
from nemsei.sync.models import SyncCursor
from nemsei.sync.production_scheduling import PRODUCTION_CURSOR_KEY


# The states, in the order they are tested. Each one names a different thing
# an operator would do next, which is the only test for whether a state
# deserves to exist separately.
STATE_OK = "ok"
STATE_NO_PROVIDER_MAPPING = "no_provider_mapping"
STATE_MAPPING_INACTIVE = "mapping_inactive"
STATE_NO_SOURCE_POLICY = "no_production_source_policy"
STATE_AMBIGUOUS_SOURCE_POLICY = "ambiguous_production_source_policy"
STATE_CONNECTION_DISABLED = "connection_disabled"
STATE_CREDENTIAL_REFERENCE_MISSING = "credential_reference_missing"
STATE_PRODUCTION_CONTRACT_MISSING = "production_contract_missing"
STATE_NOT_SCHEDULED = "scheduler_not_enabled_for_connection"
STATE_NOT_INITIALIZED = "production_not_initialized"
STATE_CURSOR_MISSING = "production_cursor_missing"
STATE_CURSOR_STALE = "production_cursor_stale"
STATE_SYNC_FAILED = "sync_failed"
STATE_RATE_LIMITED = "rate_limited"
STATE_SYNC_DEFERRED = "sync_deferred"
STATE_NO_RECENT_FACT = "no_recent_fact"
STATE_UNKNOWN = "unknown"

COVERAGE_STATES = (
    STATE_OK,
    STATE_NO_PROVIDER_MAPPING,
    STATE_MAPPING_INACTIVE,
    STATE_NO_SOURCE_POLICY,
    STATE_AMBIGUOUS_SOURCE_POLICY,
    STATE_CONNECTION_DISABLED,
    STATE_CREDENTIAL_REFERENCE_MISSING,
    STATE_PRODUCTION_CONTRACT_MISSING,
    STATE_NOT_SCHEDULED,
    STATE_NOT_INITIALIZED,
    STATE_CURSOR_MISSING,
    STATE_CURSOR_STALE,
    STATE_SYNC_FAILED,
    STATE_RATE_LIMITED,
    STATE_SYNC_DEFERRED,
    STATE_NO_RECENT_FACT,
    STATE_UNKNOWN,
)

# How many days without a daily fact before an otherwise healthy installation
# counts as not receiving production. Three, not one: the sync runs daily
# against a rate-limited account and covers provider-local D-1, so a single
# missing day is an ordinary deferral rather than a fault, and calling it one
# would make this screen cry wolf every morning.
RECENT_FACT_TOLERANCE_DAYS = 3


@dataclass(frozen=True)
class ProductionCoverage:
    """One installation's answer, with the evidence it was derived from."""

    asset_id: int
    asset_name: str
    state: str
    # What to do about it, in the operator's own terms. Deliberately part of
    # the finding rather than a lookup in the template: the state and the
    # action have to move together or the screen starts recommending fixes
    # for conditions that changed.
    recommended_action: str
    provider_code: str | None = None
    connection_id: int | None = None
    connection_name: str | None = None
    mapping_status: str | None = None
    has_source_policy: bool = False
    primary_mapping_id: int | None = None
    scheduled: bool = False
    cursor_last_completed_day: date | None = None
    last_sync_status: str | None = None
    last_sync_at: datetime | None = None
    last_sync_error_code: str | None = None
    last_production_day: date | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.state == STATE_OK


def _production_contract_present(connection: ProviderConnection) -> bool:
    """Whether the operator-verified production contract is configured.

    Mirrors `integrations/fusionsolar/production.production_contract_for`'s
    own preconditions without importing it (this module must not depend on a
    provider adapter) and without reading the values back out: the answer is
    a boolean, so nothing about the account can leak through this screen.
    """
    reference = connection.credential_reference or ""
    if not reference or not reference.replace("_", "").isalnum():
        return False
    prefix = f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}"
    return bool(os.environ.get(f"{prefix}_PRODUCTION_TIMEZONE", "").strip()) and (
        os.environ.get(f"{prefix}_PRODUCTION_UNIT", "").strip() == "kWh"
    )


def _latest_production_day(session: Session, *, asset_ids: list[int], since: date) -> dict[int, date]:
    """Each asset's newest day carrying a real production value, in one query.

    Reduced to the current revision first (`DISTINCT ON (provider_mapping_id,
    source_fact_key)` ordered by `source_revision DESC`, the same reduction
    `CanonicalFactRepository` applies), then filtered to rows that actually
    carry a value. Without the reduction, a day later corrected to
    `quality='missing'` would still count as a day with production, because
    the superseded row still exists -- `production_facts` is append-only and
    a correction never deletes what it replaces.
    """
    if not asset_ids:
        return {}
    current = (
        select(
            ProductionFact.asset_id.label("asset_id"),
            ProductionFact.period_start.label("period_start"),
            ProductionFact.value.label("value"),
        )
        .where(
            ProductionFact.asset_id.in_(asset_ids),
            ProductionFact.metric_kind == "production_energy",
            ProductionFact.granularity == "day",
            ProductionFact.period_start >= datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc),
        )
        .distinct(ProductionFact.provider_mapping_id, ProductionFact.source_fact_key)
        .order_by(
            ProductionFact.provider_mapping_id,
            ProductionFact.source_fact_key,
            ProductionFact.source_revision.desc(),
        )
        .subquery()
    )
    rows = session.execute(
        select(current.c.asset_id, func.max(current.c.period_start))
        .where(current.c.value.is_not(None))
        .group_by(current.c.asset_id)
    ).all()
    return {int(asset_id): as_utc(newest).date() for asset_id, newest in rows}


def _last_production_sync_runs(session: Session, *, connection_ids: list[int]) -> dict[int, SyncRun]:
    """The newest finished production run per connection, in one query."""
    if not connection_ids:
        return {}
    newest = (
        select(SyncRun)
        .where(
            SyncRun.provider_connection_id.in_(connection_ids),
            SyncRun.capability == ProviderCapability.PRODUCTION_HISTORY.value,
        )
        .distinct(SyncRun.provider_connection_id)
        .order_by(SyncRun.provider_connection_id, SyncRun.started_at.desc(), SyncRun.id.desc())
    )
    return {run.provider_connection_id: run for run in session.scalars(newest).all()}


def _active_plant_mappings(session: Session, *, asset_ids: list[int], on: date) -> dict[int, list[AssetProviderMapping]]:
    statement: Select = select(AssetProviderMapping).where(
        AssetProviderMapping.asset_id.in_(asset_ids),
        AssetProviderMapping.resource_kind == "plant",
        AssetProviderMapping.valid_from <= on,
        (AssetProviderMapping.valid_to.is_(None)) | (AssetProviderMapping.valid_to >= on),
    )
    grouped: dict[int, list[AssetProviderMapping]] = {}
    for mapping in session.scalars(statement).all():
        grouped.setdefault(mapping.asset_id, []).append(mapping)
    return grouped


def _active_production_policies(session: Session, *, asset_ids: list[int], on: date) -> dict[int, list[AssetSourcePolicy]]:
    statement = select(AssetSourcePolicy).where(
        AssetSourcePolicy.asset_id.in_(asset_ids),
        AssetSourcePolicy.source_use == "production",
        AssetSourcePolicy.valid_from <= on,
        (AssetSourcePolicy.valid_to.is_(None)) | (AssetSourcePolicy.valid_to >= on),
    ).order_by(AssetSourcePolicy.is_fallback.asc(), AssetSourcePolicy.priority.asc(), AssetSourcePolicy.id.asc())
    grouped: dict[int, list[AssetSourcePolicy]] = {}
    for policy in session.scalars(statement).all():
        grouped.setdefault(policy.asset_id, []).append(policy)
    return grouped


def assess_production_coverage(
    session: Session,
    *,
    asset_ids: list[int] | None = None,
    on: date | None = None,
    max_incremental_gap_days: int = 31,
) -> list[ProductionCoverage]:
    """Why each installation is, or is not, receiving daily production.

    `asset_ids=None` means the whole fleet. Six queries either way.
    """
    today = on or utc_now().date()
    asset_rows = session.execute(
        select(Asset.id, Asset.canonical_name).where(Asset.id.in_(asset_ids)) if asset_ids is not None
        else select(Asset.id, Asset.canonical_name)
    ).all()
    names = {int(row[0]): row[1] for row in asset_rows}
    if not names:
        return []
    ids = sorted(names)

    mappings = _active_plant_mappings(session, asset_ids=ids, on=today)
    policies = _active_production_policies(session, asset_ids=ids, on=today)
    connections = {
        connection.id: connection
        for connection in session.scalars(select(ProviderConnection)).all()
    }
    cursors = {
        cursor.provider_connection_id: dict(cursor.checkpoint_json or {})
        for cursor in session.scalars(
            select(SyncCursor).where(
                SyncCursor.capability == ProviderCapability.PRODUCTION_HISTORY.value,
                SyncCursor.cursor_key == PRODUCTION_CURSOR_KEY,
            )
        ).all()
    }
    scheduled_keys = {
        row.schedule_key
        for row in session.scalars(
            select(ScheduleState).where(ScheduleState.schedule_key.like("production.%"))
        ).all()
    }
    last_runs = _last_production_sync_runs(session, connection_ids=sorted(connections))
    last_days = _latest_production_day(session, asset_ids=ids, since=today - timedelta(days=400))

    findings = []
    for asset_id in ids:
        findings.append(
            _assess(
                asset_id=asset_id,
                asset_name=names[asset_id],
                mappings=mappings.get(asset_id, []),
                policies=policies.get(asset_id, []),
                connections=connections,
                cursors=cursors,
                scheduled_keys=scheduled_keys,
                last_runs=last_runs,
                last_production_day=last_days.get(asset_id),
                today=today,
                max_incremental_gap_days=max_incremental_gap_days,
            )
        )
    return findings


def _assess(
    *,
    asset_id: int,
    asset_name: str,
    mappings: list[AssetProviderMapping],
    policies: list[AssetSourcePolicy],
    connections: dict[int, ProviderConnection],
    cursors: dict[int, dict[str, Any]],
    scheduled_keys: set[str],
    last_runs: dict[int, SyncRun],
    last_production_day: date | None,
    today: date,
    max_incremental_gap_days: int,
) -> ProductionCoverage:
    """The chain, walked once, stopping at the first broken link."""

    def finding(state: str, action: str, **extra: Any) -> ProductionCoverage:
        return ProductionCoverage(
            asset_id=asset_id,
            asset_name=asset_name,
            state=state,
            recommended_action=action,
            last_production_day=last_production_day,
            **extra,
        )

    if not mappings:
        return finding(
            STATE_NO_PROVIDER_MAPPING,
            "Mapear a central a uma estação do provider em /mappings.",
        )
    active = [mapping for mapping in mappings if mapping.mapping_status == "active"]
    if not active:
        return finding(
            STATE_MAPPING_INACTIVE,
            "Rever o mapping: existe, mas nenhum está ativo. Aprovar ou corrigir em /mappings.",
            mapping_status=sorted({mapping.mapping_status for mapping in mappings})[0],
        )

    # Which mapping production reads from is the source policy's decision, and
    # this module asks the same question `resolve_source_policy` asks -- it
    # does not invent a second answer. What it does differently is refuse to
    # raise: an ambiguity is a finding to report, not an exception to escape.
    primaries = [policy for policy in policies if not policy.is_fallback]
    if not primaries:
        return finding(
            STATE_NO_SOURCE_POLICY,
            "Criar uma política de fonte de produção para esta central em /source-policies.",
            mapping_status="active",
        )
    top_priority = primaries[0].priority
    competing = [policy for policy in primaries if policy.priority == top_priority]
    if len(competing) != 1:
        return finding(
            STATE_AMBIGUOUS_SOURCE_POLICY,
            f"{len(competing)} políticas primárias com a mesma prioridade: reconciliar em /source-policies.",
            mapping_status="active",
            has_source_policy=True,
        )
    primary = competing[0]
    mapping_by_id = {mapping.id: mapping for mapping in mappings}
    mapping = mapping_by_id.get(primary.provider_mapping_id)
    connection = connections.get(mapping.provider_connection_id) if mapping is not None else None
    if connection is None:
        return finding(
            STATE_UNKNOWN,
            "A política primária aponta para um mapping que esta central não tem ativo hoje; rever em /source-policies.",
            mapping_status="active",
            has_source_policy=True,
            primary_mapping_id=primary.provider_mapping_id,
        )

    base = {
        "provider_code": connection.provider_code,
        "connection_id": connection.id,
        "connection_name": connection.display_name,
        "mapping_status": "active",
        "has_source_policy": True,
        "primary_mapping_id": primary.provider_mapping_id,
    }
    run = last_runs.get(connection.id)
    checkpoint = cursors.get(connection.id, {})
    last_day_value = checkpoint.get("last_completed_day")
    cursor_day: date | None = None
    if isinstance(last_day_value, str):
        try:
            cursor_day = date.fromisoformat(last_day_value)
        except ValueError:
            cursor_day = None
    scheduled = (
        f"production.incremental:{connection.id}" in scheduled_keys
        or f"production.bootstrap:{connection.id}" in scheduled_keys
    )
    base_evidence = {
        **base,
        "scheduled": scheduled,
        "cursor_last_completed_day": cursor_day,
        "last_sync_status": run.status if run else None,
        "last_sync_at": as_utc(run.started_at) if run else None,
        "last_sync_error_code": run.error_code if run else None,
    }

    if not connection.enabled or connection.configuration_status != "configured":
        return finding(
            STATE_CONNECTION_DISABLED,
            "Ligação ao provider desativada ou por configurar: ver /system.",
            **base_evidence,
        )
    if not connection.credential_reference:
        return finding(
            STATE_CREDENTIAL_REFERENCE_MISSING,
            "A ligação não tem referência de credencial configurada: ver /system.",
            **base_evidence,
        )
    if connection.provider_code == ProviderCode.FUSIONSOLAR.value and not _production_contract_present(connection):
        return finding(
            STATE_PRODUCTION_CONTRACT_MISSING,
            "Falta o fuso horário e a unidade kWh verificados para esta conta no ambiente do worker.",
            **base_evidence,
        )
    if cursor_day is None:
        # Two very different situations, and the difference is exactly what
        # decides what the operator does next.
        if connection.initial_production_from_date is None:
            return finding(
                STATE_NOT_INITIALIZED,
                "Produção não inicializada: indicar a data inicial da ligação para o primeiro backfill.",
                **base_evidence,
            )
        return finding(
            STATE_CURSOR_MISSING,
            f"Bootstrap por correr desde {connection.initial_production_from_date.isoformat()}; "
            "confirmar que a sincronização de produção está ligada nesta ligação.",
            **base_evidence,
        )
    if not scheduled:
        return finding(
            STATE_NOT_SCHEDULED,
            "Ligação sem agendamento de produção: ligar a sincronização de produção nesta ligação.",
            **base_evidence,
        )
    if run is not None and run.status == "rate_limited":
        return finding(
            STATE_RATE_LIMITED,
            "O provider recusou por limite de chamadas; a corrida seguinte recupera sozinha.",
            **base_evidence,
        )
    if run is not None and run.status == "deferred":
        return finding(
            STATE_SYNC_DEFERRED,
            "Sincronização adiada por cooldown da conta; nada a fazer além de esperar.",
            **base_evidence,
        )
    if (today - timedelta(days=1) - cursor_day).days > max_incremental_gap_days:
        return finding(
            STATE_CURSOR_STALE,
            f"O cursor está em {cursor_day.isoformat()}, mais atrás do que uma corrida incremental pode cobrir: "
            "correr um bounded backfill para fechar o intervalo.",
            **base_evidence,
        )
    if run is not None and run.status == "failed":
        return finding(
            STATE_SYNC_FAILED,
            f"Última sincronização falhou ({run.error_code or 'sem código'}): ver /system.",
            **base_evidence,
        )
    if last_production_day is None or (today - last_production_day).days > RECENT_FACT_TOLERANCE_DAYS:
        return finding(
            STATE_NO_RECENT_FACT,
            "A ligação está saudável mas esta central não recebe factos: confirmar que o código de estação "
            "do mapping ainda existe na conta do provider.",
            **base_evidence,
        )
    return finding(STATE_OK, "Nada a fazer.", **base_evidence)


def coverage_summary(findings: list[ProductionCoverage]) -> dict[str, Any]:
    """The counts at the top of the screen, in the order they matter.

    `with_recent_production` is the count of `ok`, not "assets with any fact
    ever" -- an installation whose last fact is four months old is not
    covered, and counting it as such is how a fleet looks healthy while a
    third of it is silent.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.state] = counts.get(finding.state, 0) + 1
    problems = {state: count for state, count in counts.items() if state != STATE_OK}
    return {
        "total": len(findings),
        "with_recent_production": counts.get(STATE_OK, 0),
        "with_problem": sum(problems.values()),
        "by_state": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
    }
