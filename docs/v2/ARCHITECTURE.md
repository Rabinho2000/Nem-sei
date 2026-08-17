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

FusionSolar currently implements only `connection_validation` and `discovery`.
Both are read-only and are unavailable unless `provider_reads=true`. A provider
connection keeps a non-secret credential reference; username and password are
read from the corresponding environment variable or mounted secret file. They
are never stored in V2 tables or SyncRun metadata.

Each network request follows: reserve persisted request state and attempt,
commit, perform the HTTP request, then persist the normalized result in a new
short transaction. The discovery flow authenticates once, reads each requested
plant-list page once, and reuses that result for mapping validation. A bounded
transient retry creates a separate request attempt; rate limits and Retry-After
defer later calls without a network request. Discovery is connection-scoped,
deduplicates stable plant identifiers, and only reports mapped/unmapped/conflict
results. It never creates assets or changes mappings.
