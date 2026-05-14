---
phase: 03-provider-integration-correctness
plan: 01
subsystem: testing
tags: [fusionsolar, provider-contracts, pytest, fixtures, production-kpi]

requires:
  - phase: 02-background-job-and-scheduler-safety
    provides: provider-neutral integration sync and FusionSolar compatibility coverage
provides:
  - pinned FusionSolar station, realtime KPI, alarm, daily KPI, and monthly KPI fixtures
  - fixture-backed FusionSolar parser and normalizer regression tests
  - explicit production key priority, raw value, timestamp, rate-limit, and session-expiry assertions
affects: [integrations, provider-sync, fusionsolar, performance, tests]

tech-stack:
  added: []
  patterns:
    - JSON provider contract fixtures under tests/fixtures/fusionsolar
    - fake session/response objects for provider fetch helper tests
    - fixture-backed production KPI key priority assertions

key-files:
  created:
    - tests/fixtures/fusionsolar/stations_page_1.json
    - tests/fixtures/fusionsolar/realtime_kpi.json
    - tests/fixtures/fusionsolar/alarms_active.json
    - tests/fixtures/fusionsolar/kpi_day_rows.json
    - tests/fixtures/fusionsolar/kpi_month_rows.json
    - tests/test_fusionsolar_provider_contracts.py
  modified: []

key-decisions:
  - "Pinned fake, reviewable FusionSolar packets instead of live provider payloads or account data."
  - "Covered current FusionSolar behavior through existing fetch, normalize, date, production, and error-recognition seams without changing production code."

patterns-established:
  - "FusionSolar contract tests load ASCII JSON fixtures from tests/fixtures/fusionsolar."
  - "Provider HTTP behavior is tested with small fake session and response objects instead of live API calls."

requirements-completed: [REQ-005, REQ-006, REQ-011]

duration: 12 min
completed: 2026-05-14
---

# Phase 03 Plan 01: FusionSolar Fixture-Backed Provider Contract Coverage Summary

**Fixture-backed FusionSolar provider contracts for response shapes, statuses, KPI timestamps, production key priority, and failCode handling**

## Performance

- **Duration:** 12 min
- **Started:** 2026-05-14T16:27:00Z
- **Completed:** 2026-05-14T16:39:00Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- Added small, fake FusionSolar JSON fixtures for station listing, realtime KPI, active alarms, daily KPI rows, and monthly KPI rows.
- Added contract tests around existing FusionSolar fetch helpers, plant-row normalization, health/alarm status mapping, collectTime parsing, and production key selection.
- Verified failCode 407 and 305 payloads raised by `post_fusionsolar_json()` are recognized by existing rate-limit and session-expiry helpers.

## Task Commits

Each task was committed atomically:

1. **Task 1: Pin realistic FusionSolar response fixtures** - `674dd12` (test)
2. **Task 2: Assert FusionSolar parsing and production assumptions from fixtures** - `51cc6b7` (test)

**Plan metadata:** recorded in the final docs commit for this plan.

## Files Created/Modified

- `tests/fixtures/fusionsolar/stations_page_1.json` - Pinned station-list packet with `data.list`, `plantCode`, and `stationCode` variants.
- `tests/fixtures/fusionsolar/realtime_kpi.json` - Pinned realtime KPI packet with healthy, faulty, and disconnected `real_health_state` values.
- `tests/fixtures/fusionsolar/alarms_active.json` - Pinned active alarm rows with minor and critical severities.
- `tests/fixtures/fusionsolar/kpi_day_rows.json` - Pinned daily KPI rows with `collectTime`, production keys, and non-production keys.
- `tests/fixtures/fusionsolar/kpi_month_rows.json` - Pinned monthly KPI rows with the same production key families.
- `tests/test_fusionsolar_provider_contracts.py` - Fixture-backed regression tests for FusionSolar parsing and production assumptions.

## Decisions Made

- Used fake provider IDs and names so fixtures are stable, non-secret, and safe to commit.
- Kept production code unchanged because the fixture-backed tests passed against existing helpers.
- Documented current local-time timestamp behavior in tests rather than changing timezone semantics without real provider evidence.

## Deviations from Plan

None - plan executed exactly as written.

**Total deviations:** 0 auto-fixed.
**Impact on plan:** No scope expansion.

## Issues Encountered

- Existing unrelated working-tree changes were present. Task commits used exact path staging so those edits were preserved and not included.
- An unrelated `03-02-SUMMARY.md` artifact is present in the worktree and was left untouched because it is outside 03-01 scope.

## User Setup Required

None - no external service configuration required.

## Verification

- `python -m json.tool tests/fixtures/fusionsolar/stations_page_1.json` - passed
- `python -m json.tool tests/fixtures/fusionsolar/realtime_kpi.json` - passed
- `python -m json.tool tests/fixtures/fusionsolar/alarms_active.json` - passed
- `python -m json.tool tests/fixtures/fusionsolar/kpi_day_rows.json` - passed
- `python -m json.tool tests/fixtures/fusionsolar/kpi_month_rows.json` - passed
- `python -m pytest -q tests/test_fusionsolar_provider_contracts.py` - 8 passed
- `python -m pytest -q tests/test_fusionsolar_provider_contracts.py tests/test_fusionsolar_service.py tests/test_performance.py tests/test_performance_backfill.py` - 45 passed
- `python -m pytest -q tests/test_fusionsolar_provider_contracts.py tests/test_fusionsolar_service.py tests/test_fusionsolar_sync.py tests/test_performance.py tests/test_performance_backfill.py` - 48 passed

## Next Phase Readiness

FusionSolar provider contract coverage is now pinned and ready to support the remaining Phase 3 provider correctness work. Coordinate with the existing worktree before advancing additional Phase 3 artifacts.

---
*Phase: 03-provider-integration-correctness*
*Completed: 2026-05-14*
