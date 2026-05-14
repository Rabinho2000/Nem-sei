---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: in_progress
last_updated: "2026-05-14T16:51:40.585Z"
progress:
  total_phases: 6
  completed_phases: 3
  total_plans: 6
  completed_plans: 6
---

# Project State

## Current Position

- Codebase map exists in `.planning/codebase/`.
- Initial project planning foundation has been created directly from the existing repository context.
- Current roadmap milestone: `Stabilize Existing Monitoring Board`.
- Completed Phase 1 Plan 01: runtime artifact and deployment hygiene.
- Completed Phase 2 Plan 01: scheduler registration and scheduled provider dispatch safety.
- Completed Phase 2 Plan 02: persisted background job startup recovery and observability.
- Completed Phase 3 Plan 01: FusionSolar fixture-backed provider contract coverage.
- Completed Phase 3 Plan 02: Sigenergy fixture-backed provider contract coverage.
- Completed Phase 3 Plan 03: provider assumptions note and phase-level regression command.
- Next recommended action: plan Phase 4 reporting/performance/alert confidence.

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
- Recover all pending background jobs on startup in ascending id order without a hidden cap.
- Leave pending job rows pending when APScheduler registration fails so future startup recovery can retry them.
- Return a small recovery summary from `schedule_pending_background_jobs()` for startup observability.
- Use fake, stable Sigenergy provider fixtures with no live secrets/customer data for contract coverage.
- Cover Sigenergy provider behavior through existing parser and sync seams before considering production-code changes.
- Use fake, stable FusionSolar provider fixtures with no live secrets/customer data for contract coverage.
- Keep FusionSolar timestamp semantics unchanged until real provider samples prove a timezone conversion bug.
- Keep the Phase 3 provider assumptions note as the boundary between fixture-pinned local behavior and unresolved live-provider contracts.
- Use the Phase 3 provider sanity command as the targeted regression gate for provider contract changes.

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
- Completed Phase 2 Plan 02 in `1b81adb` and `eefe653`; summary at `.planning/phases/02-background-job-and-scheduler-safety/02-02-SUMMARY.md`.
- Verified Phase 2 in `.planning/phases/02-background-job-and-scheduler-safety/02-VERIFICATION.md`.
- Completed Phase 3 Plan 01 in `674dd12` and `51cc6b7`; summary at `.planning/phases/03-provider-integration-correctness/03-01-SUMMARY.md`.
- Completed Phase 3 Plan 02 in `54ae413` and `3b2b425`; summary at `.planning/phases/03-provider-integration-correctness/03-02-SUMMARY.md`.
- Completed Phase 3 Plan 03 in `0e1ed2c` and `528c037`; summary at `.planning/phases/03-provider-integration-correctness/03-03-SUMMARY.md`.
- Verified Phase 3 in `.planning/phases/03-provider-integration-correctness/03-VERIFICATION.md`.

## Session Continuity

- Stopped at: Phase 3 verified complete; Phase 4 is next.
- Resume file: None.
