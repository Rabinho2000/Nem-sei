---
phase: 02-background-job-and-scheduler-safety
plan: 02
subsystem: background-jobs
tags: [flask, sqlite, apscheduler, startup-recovery, background-jobs]

requires:
  - phase: 02-background-job-and-scheduler-safety
    provides: deterministic recurring scheduler registration and provider dispatch safety
provides:
  - deterministic pending background job startup recovery
  - stale running background job failure recovery before rescheduling
  - recovery summary output for scheduled and failed pending registrations
affects: [background-jobs, scheduler, sqlite, performance-jobs]

tech-stack:
  added: []
  patterns:
    - persisted SQLite queue state remains the source of truth
    - APScheduler date jobs are recovery triggers only
    - startup recovery returns a small observable summary dictionary

key-files:
  created:
    - tests/test_background_job_recovery.py
  modified:
    - app.py

key-decisions:
  - "Startup recovery schedules every pending background job ordered by id, with no hidden LIMIT."
  - "Pending rows stay pending when APScheduler registration fails so a future recovery can retry them."
  - "schedule_pending_background_jobs() returns recovery counts while preserving callers that ignore the return value."

patterns-established:
  - "Background recovery tests use tmp_path SQLite databases plus monkeypatched schedule_background_job()."
  - "Recovery summaries include stale_running_failed, pending_found, pending_scheduled, and pending_schedule_failed_ids."

requirements-completed: [REQ-004, REQ-005, REQ-009]

duration: 40 min
completed: 2026-05-14
---

# Phase 02 Plan 02: Persisted Background Job Startup Recovery Summary

**Deterministic SQLite-backed background job recovery with visible stale, scheduled, and failed-registration outcomes**

## Performance

- **Duration:** 40 min
- **Started:** 2026-05-14T15:08:00Z
- **Completed:** 2026-05-14T15:48:23Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Removed the hard startup recovery cap so all persisted pending background jobs are considered in ascending id order.
- Kept stale running job recovery ahead of pending scheduling and preserved pending-to-running ownership in `run_background_job()`.
- Added a recovery summary return value with counts for stale, found, scheduled, and failed-to-register jobs.
- Added focused tmp SQLite tests for more-than-ten pending jobs, stale running recovery, fresh running preservation, and failed registration retry behavior.

## Task Commits

Each task was committed atomically:

1. **Task 1: Recover every pending background job on startup** - `1b81adb` (feat)
2. **Task 2: Return and log recovery outcomes** - `eefe653` (feat)

**Plan metadata:** committed separately after summary/state/roadmap updates.

## Files Created/Modified

- `app.py` - Pending job recovery query helper, all-pending startup scheduling, recovery summary output, and concise recovery logging.
- `tests/test_background_job_recovery.py` - Deterministic startup recovery tests using tmp_path SQLite and monkeypatched scheduler registration.

## Decisions Made

- Kept SQLite as the durable source of truth and APScheduler as the in-process trigger only.
- Returned a dictionary from `schedule_pending_background_jobs()` instead of introducing a new table, service, or external queue.
- Left pending rows unchanged when scheduler registration fails so restart recovery can retry them.

## Deviations from Plan

None - plan behavior was executed as written.

**Total deviations:** 0 auto-fixed.
**Impact on plan:** No scope expansion beyond the requested recovery behavior and tests.

## Issues Encountered

- The worktree already contained substantial uncommitted `app.py` changes, including prerequisite background-job code that this plan built on. Task commits were staged only for `app.py` and `tests/test_background_job_recovery.py`; unrelated dirty files were left untouched.

## User Setup Required

None - no external service configuration required.

## Verification

- `python -m pytest -q tests/test_background_job_recovery.py -k pending` - 1 passed
- `python -m pytest -q tests/test_background_job_recovery.py tests/test_db_helpers.py tests/test_performance_debug.py` - 14 passed

## Next Phase Readiness

Phase 02 is complete. Background job startup recovery and recurring scheduler registration now have focused regression coverage, ready for Phase 3 provider integration correctness planning.

---
*Phase: 02-background-job-and-scheduler-safety*
*Completed: 2026-05-14*
