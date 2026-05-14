# Project State

## Current Position

- Codebase map exists in `.planning/codebase/`.
- Initial project planning foundation has been created directly from the existing repository context.
- Current roadmap milestone: `Stabilize Existing Monitoring Board`.
- Completed Phase 1 Plan 01: runtime artifact and deployment hygiene.
- Next recommended action: `$gsd-progress` to select the next plan or phase.

## Key Decisions

- Keep the current Flask + SQLite + APScheduler architecture.
- Keep Docker Compose and Raspberry Pi 5 as the target deployment model.
- Preserve the one Gunicorn worker assumption because APScheduler runs in-process.
- Treat SQLite WAL, SHM, and rollback journal sidecars as runtime artifacts excluded from Git and Docker build context.
- Keep Docker Compose path semantics explicit: host `./data`, container `/data`, Compose `DATA_DIR=/data`, host backup `DATA_DIR=./data`.
- Gunicorn threads are acceptable for the Raspberry Pi deployment because they remain inside one worker process.
- Avoid Celery, Redis, Postgres, Kubernetes, or similar infrastructure.
- Use focused tests for risky behavior changes.
- Treat provider API behavior, timestamps, units, and rate limits as assumptions requiring verification.

## Known Context

- Main application entry point: `app.py`.
- SQLite helpers: `monitoring_board/db.py`.
- Runtime path handling: `monitoring_board/runtime.py`.
- Authentication: `monitoring_board/routes/auth.py`.
- Field route planning: `monitoring_board/routes/field_routes.py`.
- Codebase risks are documented in `.planning/codebase/CONCERNS.md`.

## Open Issues / Watch List

- `app.py` is large and mixes many concerns; avoid broad refactors.
- SQLite writes can come from requests, scheduler jobs, and background jobs.
- Scheduler behavior depends on single-process deployment.
- FusionSolar and Sigenergy API assumptions need realistic fixture coverage.
- Local runtime files must remain uncommitted; SQLite sidecars are now ignored by Git and Docker build context.

## Recent Activity

- Created `.planning/codebase/` map and committed it in `336888a`.
- Created initial `PROJECT.md`, `REQUIREMENTS.md`, `ROADMAP.md`, `STATE.md`, and `config.json` for GSD planning.
- Completed Phase 1 Plan 01 in `2b8d717` and `d162c4a`; summary at `.planning/phases/01-runtime-artifact-and-deployment-hygiene/01-01-SUMMARY.md`.

## Session Continuity

- Stopped at: Completed `01-01-PLAN.md`.
- Resume file: None.
