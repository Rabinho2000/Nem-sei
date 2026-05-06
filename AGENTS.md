# AGENTS.md

Guidance for coding agents working in this repository.

## Project Context

This repository is a Flask monitoring board for PV O&M. It uses:

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
- Do not introduce Celery, Redis, Postgres, Kubernetes, or similar infrastructure at this stage.
- Do not commit `.env`, database files, logs, uploads, Excel files, PDFs, generated backups, or other local/runtime artifacts.

## Development Guidance

- Read the surrounding code before changing behavior.
- Follow existing Flask, SQLite, APScheduler, template, and static asset patterns.
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
