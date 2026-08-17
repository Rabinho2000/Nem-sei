# Nem-sei V2 architecture

V2 is a Flask modular monolith. Web, scheduler, and one worker run as separate
processes against one V2-only PostgreSQL database. Routes call services, services
call repositories, and repositories call the database. Scheduler only enqueues
jobs; worker claims and executes them. V2 never imports V1.

Persist timestamps in UTC and render dates/times in Europe/Lisbon at the UI
boundary. V2 migrations are Alembic-only and run explicitly before application
roles start.

## Deployment and PostgreSQL operating rules

Production and preview start only through `scripts/v2_compose_up.sh`; direct
`docker compose up` is unsupported because it bypasses the explicit migration
sequence and host-artifact-root validation. Those deployments use the fixed host root
`/opt/server/apps/Nem-sei-v2-data`, physically separate from V1's
`/opt/server/apps/Nem-sei/data`. Local development must opt in with
`NEMSEI_V2_DEPLOYMENT_MODE=development` and an explicitly chosen, disjoint
host root.

The canonical script validates real paths, starts a private PostgreSQL service,
waits for its health check, runs the one-shot `migrate` role, then starts web,
scheduler, and exactly one worker. PostgreSQL safely supports future multiple
worker claims through row locking, but deployment remains deliberately limited
to one worker at this checkpoint.

`docker build -f Dockerfile.v2 .` produces the minimal runtime image. The
crash-recovery harness is acceptance-only: it explicitly builds the
`acceptance` target through `docker-compose.v2.acceptance.yml` with the
`acceptance` profile. That Compose file must never be included in a normal
deployment command.

## Job operation rules

`queued` and `waiting` jobs can be cancelled immediately. Cancelling `running`
work records a cooperative cancellation request; a handler must define where it
checks that request and how it safely stops. A lease does not make side effects
exactly once. `job_events` are append-only at both repository and PostgreSQL levels
and must contain only allowlisted, non-sensitive metadata.

## FusionSolar read slice

FusionSolar currently implements `connection_validation`, `discovery`, guarded
current monitoring, and guarded daily production history. All are read-only and
are unavailable unless `provider_reads=true`. A provider
connection keeps a non-secret credential reference; username and password are
read from the corresponding environment variable or mounted secret file. They
are never stored in V2 tables or SyncRun metadata.

## Sigenergy read slice

Sigenergy connection validation, system discovery, and current energy-flow
monitoring live under `src/nemsei/integrations/sigenergy/`. The adapter uses
the same provider-neutral SyncRun, request-attempt, IntegrationHealth,
AssetProviderMapping, AssetSourcePolicy, MonitoringObservation, and
MonitoringCurrentState services as FusionSolar. It does not add a provider
table, queue, scheduler, or canonical Sigenergy fields. Production history is
not enabled until its source-day/timezone and bounded-request contract is
verified.

Each network request follows: reserve persisted request state and attempt,
commit, perform the HTTP request, then persist the normalized result in a new
short transaction. The discovery flow authenticates once, reads each requested
plant-list page once, and reuses that result for mapping validation. A bounded
transient retry creates a separate request attempt; rate limits and Retry-After
defer later calls without a network request. Discovery is connection-scoped,
deduplicates stable plant identifiers, and only reports mapped/unmapped/conflict
results. It never creates assets or changes mappings.

## FusionSolar current monitoring

The current-monitoring slice calls `/thirdData/getStationRealKpi` only after a
single controlled login, with selected plant IDs in batches of at most 100. It
does not run discovery, alarms, device monitoring, or production requests.
Only source-policy-selected mappings are eligible. The verified plant health
codes are `3` (operational), `2` (fault), and `1` (offline); an absent or
unverified value is persisted as `unknown`, never inferred from a provider or
transport error. No verified warning code is currently known for this endpoint,
so warning remains unavailable until a documented provider capability supplies
that evidence.

The response fixtures contain no reliable provider observation timestamp. V2
therefore records the receipt time with metadata
`observed_at_source=ingested_at_no_provider_timestamp` and `freshness=unknown`.
Repeated identical current-state evidence is idempotent; a status correction
creates an append-only revision. A timeout, authentication failure, missing row,
or rate limit updates IntegrationHealth and SyncRun evidence but never writes an
offline observation or erases the last valid observation.

Canonical observations stay append-only. `monitoring_current_states` is the
separate provider-neutral, per-mapping confirmation projection: it records the
latest canonical observation, last batch attempt, last confirmed provider
evidence, and the SyncRun that last confirmed that mapping. An identical poll
does not add another observation revision, but does advance its confirmation.
A mapping absent from a partial response, or any provider failure, cannot
advance `last_confirmed_at`.

FusionSolar request attempts are finalized even when an unexpected operation
exception occurs. The audit contains only a fixed internal-failure message and
the original exception is re-raised; local evidence handling never triggers an
additional provider HTTP request.

## FusionSolar daily production history

Production history is limited to `/thirdData/getKpiStationDay`: one guarded
request per explicit provider day and up to 100 source-policy-selected plants,
after one guarded login per sync. No range/pagination behavior is claimed or
used. The adapter requires an explicit per-connection IANA source timezone and
an operator-verified `kWh` declaration for `PVYield`; without both it makes no
provider request and writes no canonical fact. It treats numeric zero as zero,
but null/absent `PVYield` as missing/partial, and never treats another V1 field
as daily production energy.

The canonical fact retains UTC period boundaries and sanitized source-period
metadata. Its per-connection daily cursor advances only after complete,
contiguous selected-mapping coverage: a successful gap window can persist facts
but cannot skip coverage. Partial, failed, deferred, rate-limited, and
historical-correction runs cannot regress it. A changed configured source
timezone stops incremental continuation until an operator reconciles/resets the
cursor. Normal sync is capped at a configurable 31 source days by default;
larger history work is deliberately deferred to explicit bounded backfill.
Re-fetching recent days is intentional reconciliation: unchanged records are
idempotent and changed evidence creates append-only fact revisions.
