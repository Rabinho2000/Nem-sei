---
phase: 02-background-job-and-scheduler-safety
plan: 01
subsystem: background-jobs
tags: [flask, sqlite, apscheduler, fusionsolar, sigenergy, scheduler]

requires:
  - phase: 01-runtime-artifact-and-deployment-hygiene
    provides: single-worker deployment assumptions and runtime artifact hygiene
provides:
  - deterministic recurring integration scheduler registration
  - provider-neutral scheduled sync dispatch wrapper
  - scheduler safety regression tests
affects: [background-jobs, integrations, provider-sync, scheduler]

tech-stack:
  added: []
  patterns:
    - stable APScheduler job IDs with replace_existing
    - provider-neutral integration sync wrapper
    - fake scheduler tests for recurring job registration

key-files:
  created:
    - tests/test_scheduler_safety.py
  modified:
    - app.py

key-decisions:
  - "Recurring provider sync jobs use stable integration-sync provider/hour IDs with max_instances=1, coalesce=True, and misfire_grace_time=1800."
  - "Scheduled and all-provider sync dispatch routes through run_integration_sync() while preserving run_fusionsolar_sync() compatibility."

patterns-established:
  - "Scheduler refresh tests use a fake scheduler object and tmp_path SQLite databases."
  - "Provider sync tests monkeypatch dispatch boundaries instead of calling external APIs."

requirements-completed: [REQ-004, REQ-005, REQ-009]

duration: 35 min
completed: 2026-05-14
---

# Phase 02 Plan 01: Scheduler Registration And Provider Dispatch Safety Summary

**Deterministic APScheduler recurring jobs and provider-neutral scheduled sync dispatch for FusionSolar and Sigenergy**

## Performance

- **Duration:** 35 min
- **Started:** 2026-05-14T12:00:00Z
- **Completed:** 2026-05-14T12:32:25Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Made recurring scheduler refresh idempotent for provider sync jobs and legacy `fusionsolar-sync-*` cleanup.
- Added explicit recurring job options: `replace_existing=True`, `max_instances=1`, `coalesce=True`, and `misfire_grace_time=1800`.
- Added `run_integration_sync()` and routed scheduled/all-provider sync dispatch through it.
- Added focused regression tests for fake scheduler registration, Sigenergy scheduled dispatch, all-provider wrapper dispatch, and persisted sync failures.

## Task Commits

Each task was committed atomically:

1. **Task 1: Make recurring scheduler registration deterministic** - `b8c62be` (feat)
2. **Task 2: Route scheduled syncs through a provider-neutral entry point** - `6e1cf07` (feat)

**Plan metadata:** committed separately after summary/state/roadmap updates.

## Files Created/Modified

- `app.py` - Scheduler recurring job options, provider-neutral scheduled dispatch wrapper, and all-provider sync dispatch through the wrapper.
- `tests/test_scheduler_safety.py` - Fake scheduler and provider dispatch regression tests.

## Decisions Made

- Kept APScheduler in-process with stable IDs and replacement instead of adding locks, persistent job stores, or external workers.
- Kept `run_fusionsolar_sync()` as the compatibility implementation and introduced `run_integration_sync()` as the neutral entry point.
- Verified provider behavior by monkeypatching dispatch functions and SQLite state rather than calling real provider APIs.

## Deviations from Plan

None - plan executed exactly as written.

**Total deviations:** 0 auto-fixed.
**Impact on plan:** No scope expansion.

## Issues Encountered

- Existing unrelated working-tree and staged changes were present. Task commits were created with an isolated git index so unrelated edits were preserved and not swept into the GSD commits.

## User Setup Required

None - no external service configuration required.

## Verification

- `python -m pytest -q tests/test_scheduler_safety.py -k scheduler` - 4 passed
- `python -m pytest -q tests/test_scheduler_safety.py tests/test_fusionsolar_sync.py` - 7 passed
- `python -m pytest -q tests/test_scheduler_safety.py tests/test_fusionsolar_sync.py tests/test_fusionsolar_service.py` - 11 passed

## Next Phase Readiness

Phase 02 Plan 02 can proceed to persisted background job startup recovery and observability with deterministic scheduler registration now covered by tests.

---
*Phase: 02-background-job-and-scheduler-safety*
*Completed: 2026-05-14*
