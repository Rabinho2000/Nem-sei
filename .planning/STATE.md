# Project State

## Current Position

- Codebase map exists in `.planning/codebase/`.
- Initial project planning foundation has been created directly from the existing repository context.
- Current roadmap milestone: `Stabilize Existing Monitoring Board`.
- Next recommended action: `$gsd-plan-phase 1`.

## Key Decisions

- Keep the current Flask + SQLite + APScheduler architecture.
- Keep Docker Compose and Raspberry Pi 5 as the target deployment model.
- Preserve the one Gunicorn worker assumption because APScheduler runs in-process.
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
- Local runtime files currently appear in the working tree and must remain uncommitted.

## Recent Activity

- Created `.planning/codebase/` map and committed it in `336888a`.
- Created initial `PROJECT.md`, `REQUIREMENTS.md`, `ROADMAP.md`, `STATE.md`, and `config.json` for GSD planning.

