"""Durable intra-day batch progress for FusionSolar production backfill.

A source day with more than `_MAX_BATCH` (100) selected mappings needs more
than one provider call. Without a durable checkpoint, every attempt at that
day restarted from batch one: whichever call the provider allowed that cycle
was spent re-fetching mappings that already had facts, and batch two -- the
one that actually needed the call -- never got its turn. Six reportable
August assets (Museu Caramulo, Maryasa, ITM Moita, Viarco, Granitos Galrão do
Norte, Mirandesa) sat missing from every affected day for exactly this
reason; most incomplete days plateaued at 36/42 rather than completing.

These tests prove the fix at two levels: the service level (`_sync_day`'s own
checkpoint mechanics, fast and precise) and the worker level (the full
job/defer/resume loop, including the zero-call-deferral invariant the
previous fix established, which this defect's fix must not disturb).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

from sqlalchemy import func, select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.client import FusionSolarClient, HttpResponse
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService
from nemsei.jobs.production import bounded_backfill_payload
from nemsei.jobs.repository import JobRepository
from nemsei.jobs.worker import Worker
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy

from tests_v2.test_migrations import upgrade
from tests_v2.test_rate_limit_deferral import (
    LOGIN_OK,
    RATE_LIMITED,
    cooldown_of,
    enabled,
    events_of,
    expire_cooldown,
    isolate_session_cache,
    job_row,
    make_due,
    production_environment,
    real_calls,
    use_transport,
)


# --- shared fixtures -----------------------------------------------------------


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, payload)


def daily(rows):
    return response({"success": True, "failCode": 0, "data": rows})


def row(code, value):
    return {"stationCode": code, "dataItemMap": {"PVYield": value}}


class BatchTransport:
    """Records calls; stamps `collectTime` the way the real endpoint would.

    An exhausted script raises `IndexError` instead of quietly answering, so
    "batch one was never re-fetched" is proved, not assumed.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def post(self, url, payload, headers, timeout_seconds):
        self.calls.append(url.rsplit("/", 1)[-1])
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if "collectTime" in payload and isinstance(value.payload, dict):
            for entry in value.payload.get("data") or []:
                if isinstance(entry, dict) and "collectTime" not in entry:
                    entry["collectTime"] = payload["collectTime"]
        return value

    def load(self, responses):
        self.responses = list(responses)


def configured_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_BASE_URL", "https://fusion.example.test")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_TIMEZONE", "UTC")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_UNIT", "kWh")


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def connection_with_mappings(factory, *, count: int) -> tuple[int, list[AssetProviderMapping]]:
    """A FusionSolar connection with `count` plant mappings, each with an
    active production source policy from 2020-01-01 -- the shape a real
    August connection has, just scaled up to force multiple batches."""
    with factory() as session:
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="fusion-batch",
            display_name="Fusion batch checkpoint",
            credential_reference="production",
            enabled=True,
            configuration_status="configured",
        )
        mappings = []
        for number in range(1, count + 1):
            asset = create_asset(session, canonical_name=f"Batch asset {number:03d}")
            mapping = create_mapping(
                session,
                asset_id=asset.id,
                provider_connection_id=connection.id,
                external_id=f"FS-{number:03d}",
                valid_from=date(2020, 1, 1),
            )
            create_source_policy(
                session,
                asset_id=asset.id,
                provider_mapping_id=mapping.id,
                source_use="production",
                priority=1,
                valid_from=date(2020, 1, 1),
            )
            mappings.append(mapping)
        session.commit()
        for mapping in mappings:
            session.expunge(mapping)
        return connection.id, mappings


def service(factory, settings, transport, *, retries=0):
    configured = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    return FusionSolarProductionService(
        factory,
        configured,
        client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport),
        max_transient_retries=retries,
    )


def fact_count(factory) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(ProductionFact)) or 0


def facts_by_mapping(factory) -> dict[int, list[int]]:
    with factory() as session:
        revisions: dict[int, list[int]] = {}
        for fact in session.scalars(select(ProductionFact)):
            revisions.setdefault(fact.provider_mapping_id, []).append(fact.source_revision)
        return revisions


def backfill_job(repository, connection_id, *, day: date, max_attempts: int = 10):
    job, created = repository.enqueue(
        job_type="production.bounded_backfill",
        payload=bounded_backfill_payload(connection_id, start_date=day, end_date=day),
        actor_source="web",
        max_attempts=max_attempts,
    )
    assert created
    return job


DAY = date(2026, 8, 7)


# --- service-level: the checkpoint mechanics ------------------------------------


def test_132_mappings_need_two_batches_and_the_second_rate_limits(settings, monkeypatch):
    """1. 132 mappings => two batches. 2. batch one succeeds. 3. batch two
    rate-limits. 4. the day defers durably -- a checkpoint is returned rather
    than the failure being indistinguishable from one that made no progress."""
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = connection_with_mappings(factory, count=132)
    transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        RATE_LIMITED,
    ])
    result = service(factory, settings, transport).sync_bounded_backfill(connection_id, start_date=DAY, end_date=DAY)

    # "partial", not "rate_limited": `_finish` reads accepted-with-an-error as
    # partial completion first, and batch one's 100 facts are real accepted
    # work. Both statuses are in `_execute_production`'s retry-triggering set
    # (matching what production's own stuck sync_runs actually showed), so
    # this does not change which path the job takes.
    assert result.status == "partial"
    checkpoint = result.batch_checkpoint
    assert checkpoint is not None
    assert checkpoint["source_day"] == DAY.isoformat()
    assert checkpoint["connection_id"] == connection_id
    assert checkpoint["mapping_count"] == 132
    assert checkpoint["batch_size"] == 100
    assert checkpoint["batch_count"] == 2
    assert checkpoint["batches_done"] == 1
    assert checkpoint["next_batch"] == 1
    assert set(checkpoint["completed_mapping_ids"]) == {m.id for m in mappings[:100]}
    # Batch one's facts are already durably persisted -- not held in memory
    # waiting for the whole day to finish.
    assert fact_count(factory) == 100


def test_resume_makes_zero_duplicate_calls_and_starts_directly_at_batch_two(settings, monkeypatch):
    """5. resume makes ZERO duplicate provider calls for batch one.
    6. resume starts directly at batch two. 7. batch two succeeds.
    8. the day becomes complete. 9. each mapping's fact exists exactly once."""
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = connection_with_mappings(factory, count=132)
    first_transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        RATE_LIMITED,
    ])
    first = service(factory, settings, first_transport).sync_bounded_backfill(connection_id, start_date=DAY, end_date=DAY)
    checkpoint = first.batch_checkpoint
    assert checkpoint is not None

    # Batch one's rate limit persisted a real cooldown; it has to have
    # expired before the provider would take a second call at all -- exactly
    # what waiting for `available_at` means in production.
    expire_cooldown(factory, connection_id)
    # This script holds only a login and batch two's response. Any attempt to
    # re-fetch batch one raises IndexError instead of quietly succeeding.
    second_transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "20.0") for m in mappings[100:]]),
    ])
    second = service(factory, settings, second_transport).sync_bounded_backfill(
        connection_id, start_date=DAY, end_date=DAY, batch_checkpoint=checkpoint,
    )

    assert [call for call in second_transport.calls] == ["login", "getKpiStationDay"]
    assert second.status == "success"
    assert second.batch_checkpoint is None
    assert fact_count(factory) == 132
    revisions = facts_by_mapping(factory)
    assert set(revisions) == {m.id for m in mappings}
    assert all(revs == [1] for revs in revisions.values()), "no mapping's fact was written more than once"


def test_100_or_fewer_mappings_still_work_normally(settings, monkeypatch):
    """12. a source day with <=100 mappings needs no checkpoint at all."""
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = connection_with_mappings(factory, count=57)
    transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "5.0") for m in mappings]),
    ])
    result = service(factory, settings, transport).sync_bounded_backfill(connection_id, start_date=DAY, end_date=DAY)

    assert result.status == "success"
    assert result.batch_checkpoint is None
    assert transport.calls == ["login", "getKpiStationDay"]
    assert fact_count(factory) == 57


def test_a_crash_before_checkpoint_advancement_may_replay_a_batch_without_corrupting_facts(settings, monkeypatch):
    """11. The checkpoint write is a separate transaction from the facts a
    batch persists. A crash between them means the next attempt does not know
    batch one already succeeded and re-fetches it -- one wasted call, and the
    persistence underneath is idempotent, so nothing duplicates or corrupts."""
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = connection_with_mappings(factory, count=132)
    transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        RATE_LIMITED,
    ])
    first = service(factory, settings, transport).sync_bounded_backfill(connection_id, start_date=DAY, end_date=DAY)
    assert first.batch_checkpoint is not None
    assert fact_count(factory) == 100

    # As if the checkpoint above was never durably written: the next attempt
    # starts the day cold, with no `batch_checkpoint` at all.
    expire_cooldown(factory, connection_id)
    replay_transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        daily([row(m.external_id, "20.0") for m in mappings[100:]]),
    ])
    second = service(factory, settings, replay_transport).sync_bounded_backfill(connection_id, start_date=DAY, end_date=DAY)

    assert second.status == "success"
    assert fact_count(factory) == 132, "the replayed batch did not duplicate its facts"
    revisions = facts_by_mapping(factory)
    assert all(revs == [1] for revs in revisions.values()), "identical replayed values stay at revision 1"


def test_a_newly_added_mapping_between_attempts_joins_the_remaining_work(settings, monkeypatch):
    """13a. Mapping validity per source day is preserved across a resume: a
    mapping that starts being selected only after batch one completed is
    still picked up -- included in what remains, not lost and not corrupting
    the already-completed set."""
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = connection_with_mappings(factory, count=132)
    first_transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        RATE_LIMITED,
    ])
    first = service(factory, settings, first_transport).sync_bounded_backfill(connection_id, start_date=DAY, end_date=DAY)
    checkpoint = first.batch_checkpoint
    assert checkpoint is not None

    with factory() as session:
        asset = create_asset(session, canonical_name="Late-added asset")
        new_mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection_id, external_id="FS-NEW", valid_from=date(2020, 1, 1),
        )
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=new_mapping.id, source_use="production", priority=1, valid_from=date(2020, 1, 1))
        session.commit()
        new_mapping_id = new_mapping.id
        new_external_id = new_mapping.external_id

    expire_cooldown(factory, connection_id)
    remaining_external_ids = {m.external_id for m in mappings[100:]} | {new_external_id}
    second_transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(external_id, "20.0") for external_id in sorted(remaining_external_ids)]),
    ])
    second = service(factory, settings, second_transport).sync_bounded_backfill(
        connection_id, start_date=DAY, end_date=DAY, batch_checkpoint=checkpoint,
    )

    assert second.status == "success", "the new mapping's own batch call still had to succeed, and it did"
    assert fact_count(factory) == 133
    revisions = facts_by_mapping(factory)
    assert new_mapping_id in revisions and revisions[new_mapping_id] == [1]


def test_a_mapping_dropped_from_selection_does_not_corrupt_resumption(settings, monkeypatch):
    """13b. A mapping already covered by batch one, then retired before
    resumption, must not block the day or resurface: resumption is keyed by
    which mapping ids are still selected, not by a frozen position."""
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = connection_with_mappings(factory, count=132)
    first_transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        RATE_LIMITED,
    ])
    first = service(factory, settings, first_transport).sync_bounded_backfill(connection_id, start_date=DAY, end_date=DAY)
    checkpoint = first.batch_checkpoint
    assert checkpoint is not None
    dropped = mappings[0]
    assert dropped.id in checkpoint["completed_mapping_ids"]

    with factory() as session:
        stored = session.get(AssetProviderMapping, dropped.id)
        stored.mapping_status = "superseded"
        session.commit()

    expire_cooldown(factory, connection_id)
    second_transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "20.0") for m in mappings[100:]]),
    ])
    second = service(factory, settings, second_transport).sync_bounded_backfill(
        connection_id, start_date=DAY, end_date=DAY, batch_checkpoint=checkpoint,
    )

    assert second.status == "success", "the retired mapping's stale progress did not block the day"
    assert [call for call in second_transport.calls] == ["login", "getKpiStationDay"], "batch two, exactly as it would have been without the drop"
    # The retired mapping's own historical fact is untouched -- append-only,
    # never deleted -- it simply stops being part of what "this day" needs.
    revisions = facts_by_mapping(factory)
    assert revisions[dropped.id] == [1]
    assert fact_count(factory) == 132


# --- worker-level: durability across the job/defer/resume loop -----------------


def test_restart_between_batch_persistence_and_job_completion_resumes_correctly(settings, monkeypatch):
    """10. A durable checkpoint survives a restart: a brand-new `Worker`
    (standing in for a fresh process) resumes directly at batch two."""
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id, mappings = connection_with_mappings(factory, count=132)
    worker_settings = replace(enabled(settings), job_defer_max_cycles=3)
    repository = JobRepository(engine, factory)

    transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        RATE_LIMITED,
    ])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job = backfill_job(repository, connection_id, day=DAY)

    assert Worker(worker_settings, worker_id="pre-restart").run_once()
    before = job_row(factory, job.id)
    assert before.status == "waiting"
    checkpoint = before.payload_json.get("backfill_batch_checkpoint")
    assert checkpoint is not None and checkpoint["batches_done"] == 1

    # A real restart loses more than the worker's in-memory state -- the
    # process-wide session cache goes with it, so the next attempt logs in
    # again rather than reusing a session that no longer exists anywhere.
    isolate_session_cache(monkeypatch)
    expire_cooldown(factory, connection_id)
    make_due(factory, job.id)
    transport.load([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row(m.external_id, "20.0") for m in mappings[100:]])])
    assert Worker(worker_settings, worker_id="post-restart").run_once()

    assert job_row(factory, job.id).status == "success"
    assert fact_count(factory) == 132


def test_a_day_completes_over_two_cooldown_cycles_with_only_one_call_each(settings, monkeypatch):
    """The production shape directly: the provider allows about one real
    call per cooldown. Cycle one spends it on batch one, batch two is
    rate-limited and the checkpoint is retained. Zero-call deferrals wait out
    the cooldown at no cost to the attempt budget (the earlier fix). Cycle two
    resumes directly at batch two -- proved from the request log that batch
    one was never called again -- and the day completes."""
    production_environment(monkeypatch)
    isolate_session_cache(monkeypatch)
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    connection_id, mappings = connection_with_mappings(factory, count=132)
    worker_settings = replace(enabled(settings), job_defer_max_cycles=3)
    repository = JobRepository(engine, factory)

    transport = BatchTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row(m.external_id, "10.0") for m in mappings[:100]]),
        RATE_LIMITED,
    ])
    use_transport(monkeypatch, "FusionSolarProductionService", transport)
    job = backfill_job(repository, connection_id, day=DAY)

    # Cycle 1: the only real call this cooldown allows is spent on batch one;
    # batch two is rate-limited and the day defers durably.
    assert Worker(worker_settings, worker_id="cycle1").run_once()
    cycle1_calls = len(transport.calls)
    row1 = job_row(factory, job.id)
    assert row1.status == "waiting"
    baseline_attempts = row1.attempt_count
    cooldown = cooldown_of(factory, connection_id)
    assert cooldown > datetime.now(timezone.utc)
    assert row1.payload_json["backfill_batch_checkpoint"]["batches_done"] == 1

    # Zero-call deferrals while the cooldown stands: attempts untouched, no
    # HTTP at all -- the invariant the previous fix established, still green.
    assert transport.responses == []
    for _ in range(3):
        make_due(factory, job.id)
        Worker(worker_settings, worker_id="cycle1-wait").run_once()
    assert job_row(factory, job.id).attempt_count == baseline_attempts
    assert len(transport.calls) == cycle1_calls, "zero HTTP while the cooldown stands"
    assert real_calls(factory, connection_id) == real_calls(factory, connection_id)  # sanity: query does not raise

    # Cycle 2: cooldown lifts, resume starts directly at batch two. The same
    # process, so the login session from cycle one is still cached -- exactly
    # like the real worker, which never logs in twice for one live session
    # (docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md). Only the batch call is new.
    transport.load([daily([row(m.external_id, "20.0") for m in mappings[100:]])])
    expire_cooldown(factory, connection_id)
    make_due(factory, job.id)
    assert Worker(worker_settings, worker_id="cycle2").run_once()

    assert len(transport.calls) == cycle1_calls + 1, "exactly one call -- the cached session, not a second login, and batch one was not repeated"
    assert job_row(factory, job.id).status == "success"
    assert fact_count(factory) == 132
    kinds = [event for event, *_ in events_of(factory, job.id)]
    assert "retry_exhausted" not in kinds
