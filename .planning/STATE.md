---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: in_progress
last_updated: "2026-05-14T12:32:25.619Z"
progress:
  total_phases: 6
  completed_phases: 1
  total_plans: 3
  completed_plans: 2
---

# Project State

## Current Position

- Codebase map exists in `.planning/codebase/`.
- Initial project planning foundation has been created directly from the existing repository context.
- Current roadmap milestone: `Stabilize Existing Monitoring Board`.
- Completed Phase 1 Plan 01: runtime artifact and deployment hygiene.
- Completed Phase 2 Plan 01: scheduler registration and scheduled provider dispatch safety.
- Next recommended action: execute or plan Phase 2 Plan 02 for persisted background job startup recovery and observability.

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
- Use stable APScheduler IDs plus `replace_existing`, `max_instances=1`, `coalesce=True`, and explicit misfire grace for recurring provider jobs.
- Route scheduled/all-provider integration syncs through `run_integration_sync()` while preserving `run_fusionsolar_sync()` compatibility.

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
- Completed Phase 2 Plan 01 in `b8c62be` and `6e1cf07`; summary at `.planning/phases/02-background-job-and-scheduler-safety/02-01-SUMMARY.md`.

## Session Continuity

- Stopped at: Completed `02-01-PLAN.md`.
- Resume file: None.
