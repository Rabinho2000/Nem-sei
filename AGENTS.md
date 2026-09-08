# AGENTS.md

Guidance for coding agents working in this repository.

## Two products live here, and the rules differ

This repository holds two systems. Which one you are in decides which rules
below apply, so establish that before anything else.

**V1 -- `monitoring_board/`, `app.py`, `tests/`.** A Flask monitoring board for
PV O&M on SQLite and APScheduler, deployed to a Raspberry Pi 5 with Docker
Compose and Cloudflare Tunnel. It is frozen: the last commit on `main` is
`docs: freeze v1 final state`, and its services are stopped. Read it as the
reference implementation of contracts nobody else has verified -- the
Sigenergy daily-history parser and the FusionSolar wire format are V1's
knowledge before they are anyone's -- and change it only to keep it running.

**V2 -- `src/nemsei/`, `migrations/`, `tests_v2/`.** The system under active
development. PostgreSQL 16, Alembic, a durable job queue with leases in the
database, and its own Compose stack (`docker-compose.v2.yml`).

The rule "do not introduce Postgres" below is V1's, and V1's only. It was
written before V2 existed and read for a while as though it forbade V2's
actual architecture. It does not: V2 runs on PostgreSQL deliberately, and
reverting that is not on the table.

## Project Context (V1)

- SQLite for persistence.
- APScheduler for background/scheduled work.
- FusionSolar API integration.
- Telegram integration.
- Excel import/export workflows.
- PDF report generation.

The intended deployment target is Raspberry Pi 5 using Docker Compose, SQLite, and Cloudflare Tunnel.

## Repository Rules

- Do not refactor unrelated logic.
- Keep changes small and easy to review.
- Preserve existing behavior unless explicitly asked to change it.
- Prefer simple solutions over new infrastructure.
- **In V1**, do not introduce Celery, Redis, Postgres, Kubernetes, or similar infrastructure.
- **In V2**, PostgreSQL and Alembic are the architecture. Do not add Kafka, Redis,
  RabbitMQ or Celery: the queue, the leases and the scheduler state are
  PostgreSQL rows on purpose, and a second messaging system would split the
  record of what was collected across two places that cannot be joined.
- Do not commit `.env`, database files, logs, uploads, Excel files, PDFs, generated backups, or other local/runtime artifacts.

## What "it works" has to mean (V2)

An explicit failure is an acceptable outcome. A false success is not, and
neither is a silent gap. Concretely, and each of these was a real defect:

- A job is `success` only when the data it was obliged to collect was
  collected and persisted. Not partial, not missing, not "the provider
  answered".
- A cursor moves only over days that were actually read. Replaying a stored
  day is cheap and idempotent; skipping one is not recoverable.
- An empty payload is not a collected day, and the day still in progress is
  not a daily total.
- One asset, one metric, one period yields one value from one source. Two
  mappings for the same day are not two readings to add.
- A missing credential is a configuration failure, never a mock that reports
  delivery.

`scripts/v2_audit_manifest.py` records what a checkout, a database and a host
actually are, read-only and without secrets. Prefer it to any number quoted in
documentation, including in this file.

## Development Guidance

- Read the surrounding code before changing behavior.
- Follow existing Flask, SQLite, APScheduler, template, and static asset
  patterns **in V1**; in V2 follow SQLAlchemy, Alembic and the job queue in
  `src/nemsei/jobs/`.
- Keep SQLite usage conservative, especially around long-running requests, background jobs, and concurrent writes.
- Be careful with scheduled jobs so they are registered only once per process/deployment.
- Treat FusionSolar API behavior, units, pagination, rate limits, token refresh, and response shapes as assumptions that must be verified before relying on them.
- Be explicit with dates, times, timezones, and unit conversions in monitoring, reporting, imports, exports, and alerts.
- Add focused tests when changing logic that affects scheduling, database writes, API parsing, calculations, reports, alerts, or imports.

## Review Guidelines

When reviewing changes, focus on production risks first:

- SQLite locking, transaction duration, concurrent writes, and connection lifecycle.
- Duplicated APScheduler jobs or jobs running in multiple processes unexpectedly.
- FusionSolar API assumptions, token handling, rate limits, pagination, missing fields, and unit conversions.
- Date, timezone, daylight-saving, and reporting period bugs.
- Incorrect energy, power, currency, percentage, or availability calculations.
- Telegram notification spam, missing throttling, or broken error handling.
- Excel import/export edge cases and schema drift.
- PDF report generation failures and missing files/assets.
- Missing tests for changed behavior.

Ignore style nitpicks unless they affect readability, maintainability, correctness, or operational safety.

## Deployment Constraints

- Assume the production environment is resource-constrained compared with a server VM.
- Avoid heavy background services and unnecessary dependencies.
- Keep Docker Compose deployment simple.
- Preserve compatibility with SQLite on a Raspberry Pi filesystem.
- Avoid changes that require cloud infrastructure beyond the existing Cloudflare Tunnel target.
