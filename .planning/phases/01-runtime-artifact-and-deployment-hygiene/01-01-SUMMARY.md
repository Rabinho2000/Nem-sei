---
phase: 01-runtime-artifact-and-deployment-hygiene
plan: 01
subsystem: infra
tags: [sqlite, docker, raspberry-pi, apscheduler, deployment]

requires:
  - phase: project-initialization
    provides: roadmap and runtime hygiene requirements
provides:
  - Git and Docker exclusions for SQLite WAL, SHM, and rollback journal sidecars
  - Deployment documentation for one-worker APScheduler operation
  - Host/container DATA_DIR path guidance for Docker Compose backups and restores
affects: [deployment, runtime-artifacts, scheduler-safety, backups]

tech-stack:
  added: []
  patterns:
    - Explicit SQLite sidecar ignore rules beside database ignore rules
    - One Gunicorn worker plus threads while APScheduler remains in-process
    - Host ./data mounted to container /data with Compose DATA_DIR=/data

key-files:
  created:
    - .dockerignore
    - docs/raspberry-pi-deployment.md
  modified:
    - .gitignore
    - README.md

key-decisions:
  - "Keep APScheduler in-process and document exactly one Gunicorn worker as a production correctness constraint."
  - "Treat SQLite WAL, SHM, and rollback journal sidecars as local runtime artifacts excluded from Git and Docker build context."
  - "Keep Docker Compose path semantics explicit: host ./data, container /data, Compose DATA_DIR=/data, host backup DATA_DIR=./data."

patterns-established:
  - "Runtime artifact hygiene: Git and Docker ignore files should mirror database sidecar exclusions."
  - "Deployment docs must warn against multi-worker or scaled app instances until scheduling is redesigned."

requirements-completed: [REQ-001, REQ-003, REQ-004, REQ-010]

duration: 2 min
completed: 2026-05-14
---

# Phase 01 Plan 01: Runtime Artifact And Deployment Hygiene Summary

**SQLite runtime sidecars are excluded from Git and Docker build context, with Raspberry Pi docs reinforcing one-worker APScheduler deployment and DATA_DIR path boundaries**

## Performance

- **Duration:** 2 min
- **Started:** 2026-05-14T11:51:32Z
- **Completed:** 2026-05-14T11:53:19Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Added explicit `*.db-wal`, `*.db-shm`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite3-wal`, and `*.sqlite3-shm` exclusions to Git and Docker ignore rules.
- Added matching rollback journal exclusions beside the same local database artifact rules.
- Tightened README and Raspberry Pi deployment guidance around in-process APScheduler, exactly one Gunicorn worker, acceptable threads, Compose scaling warnings, and `DATA_DIR` host/container usage.

## Task Commits

Each task was committed atomically:

1. **Task 1: Ignore SQLite WAL and SHM runtime sidecars** - `2b8d717` (chore)
2. **Task 2: Tighten Raspberry Pi deployment documentation** - `d162c4a` (docs)

**Plan metadata:** committed separately by the final GSD metadata commit.

## Files Created/Modified

- `.gitignore` - Added SQLite WAL, SHM, and rollback journal sidecar exclusions beside existing database ignores.
- `.dockerignore` - Added matching Docker build-context exclusions for local database files and SQLite sidecars.
- `README.md` - Documented one-worker APScheduler constraint, acceptable Gunicorn threads, scaling warnings, `DATA_DIR` path story, and SQLite sidecar local-file guidance.
- `docs/raspberry-pi-deployment.md` - Added Raspberry Pi deployment guidance for one-worker Gunicorn, Compose scaling avoidance, host/container data paths, backup/restore commands, and sidecar-aware SQLite backup language.

## Decisions Made

- Kept runtime code, Docker command behavior, and Compose behavior unchanged in the plan commits.
- Included rollback journal patterns because they fit the same local SQLite database artifact section and do not broaden the ignore scope beyond runtime database sidecars.
- Documented threads as acceptable because `--threads 4` stays within one Gunicorn worker process while APScheduler remains in-process.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The worktree already contained unrelated staged and unstaged runtime/deployment changes before this plan started. The requested `git diff -- Dockerfile docker-compose.yml app.py monitoring_board/runtime.py` check was not empty because of those pre-existing changes, but `git diff --name-only HEAD~2..HEAD -- Dockerfile docker-compose.yml app.py monitoring_board/runtime.py` was empty, confirming this plan's commits did not change runtime code, Docker command behavior, or Compose behavior.

## Verification

- `git check-ignore -v monitoring_board.db monitoring_board.db-wal monitoring_board.db-shm monitoring_board.sqlite-wal monitoring_board.sqlite-shm monitoring_board.sqlite3-wal monitoring_board.sqlite3-shm` - passed.
- `Select-String -Path .dockerignore -Pattern '\*.db-wal','\*.db-shm','\*.sqlite-wal','\*.sqlite-shm','\*.sqlite3-wal','\*.sqlite3-shm'` - passed.
- `Select-String -Path README.md,docs/raspberry-pi-deployment.md -Pattern 'APScheduler','DATA_DIR','/data','./data','--scale','db-wal','db-shm'` - passed.
- `Select-String -Path Dockerfile,docker-compose.yml -Pattern 'gunicorn.*-w 1','DATA_DIR: /data','./data:/data'` - passed.
- `python -m pytest -q tests/test_runtime_paths.py` - passed, 4 tests.
- `git diff --name-only HEAD~2..HEAD -- Dockerfile docker-compose.yml app.py monitoring_board/runtime.py` - passed, no files changed by this plan's commits.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 1 plan 01 is complete. The repository is ready for the next runtime/deployment hygiene plan or for Phase 2 scheduler safety planning, with the existing dirty worktree still preserved for unrelated work.

---
*Phase: 01-runtime-artifact-and-deployment-hygiene*
*Completed: 2026-05-14*
