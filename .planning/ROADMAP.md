# Roadmap

## Milestone: Stabilize Existing Monitoring Board

This roadmap assumes the current product is already useful and should be hardened before larger feature expansion. Phases are intentionally small enough for focused planning and execution.

### Phase 1: Runtime Artifact And Deployment Hygiene

**Goal:** Keep local/runtime files out of Git and make the Raspberry Pi deployment assumptions explicit and enforceable.

**Requirements:** REQ-001, REQ-003, REQ-004, REQ-010

**Progress:** Complete - 1/1 plans complete as of 2026-05-14.

**Scope:**

- Ensure `.gitignore` covers SQLite sidecars such as `*.db-wal` and `*.db-shm`.
- Review Docker and README guidance for one-worker APScheduler deployment.
- Confirm runtime paths use `DATA_DIR` consistently in Docker.
- Add or update tests only where behavior changes.

**Success Criteria:**

- Runtime database sidecars no longer appear as untracked Git candidates.
- Deployment docs clearly warn against multiple workers/processes unless scheduling is redesigned.
- No app behavior changes beyond hygiene and documentation.

### Phase 2: Background Job And Scheduler Safety

**Goal:** Make scheduled syncs and persisted background jobs easier to reason about and less likely to duplicate, stall, or silently fail.

**Requirements:** REQ-004, REQ-005, REQ-009

**Scope:**

- Audit APScheduler registration and startup recovery paths.
- Verify scheduled provider dispatch for FusionSolar and Sigenergy.
- Improve stale/pending background job recovery where needed.
- Add focused tests for job registration, duplicate prevention, and recovery behavior.

**Success Criteria:**

- Scheduler behavior remains single-process and deterministic.
- Pending/stale jobs are handled predictably after restart.
- Provider-specific scheduled sync behavior is covered by tests.

### Phase 3: Provider Integration Correctness

**Goal:** Reduce risk around FusionSolar and Sigenergy parsing, timestamps, rate limits, and production units.

**Requirements:** REQ-005, REQ-006, REQ-011

**Scope:**

- Pin realistic mocked API fixtures for key provider responses.
- Verify status mapping, production value selection, timestamp conversion, and rate-limit/session-expiry handling.
- Document any remaining provider assumptions in code or planning notes.

**Success Criteria:**

- Critical provider response shapes have regression tests.
- Date and production-unit assumptions are explicit.
- Existing manual and scheduled sync behavior remains compatible.

### Phase 4: Reporting, Performance, And Alert Confidence

**Goal:** Improve confidence in calculations and user-facing outputs that affect operational decisions.

**Requirements:** REQ-005, REQ-011, REQ-012

**Scope:**

- Add regression coverage for production performance calculations and reporting periods.
- Test alert throttling and notification edge cases around repeated failures and recoveries.
- Harden PDF/XLSX export failure handling where gaps are found.

**Success Criteria:**

- Calculation and report changes are backed by focused tests.
- Telegram alert spam controls remain intact.
- Export/report failures are user-visible and do not corrupt data.

### Phase 5: Import And Data Integrity Guardrails

**Goal:** Make Excel and manual monitoring imports safer against schema drift, incomplete data, and accidental overwrites.

**Requirements:** REQ-002, REQ-005, REQ-012

**Scope:**

- Review Excel sheet and column assumptions.
- Add validation or warnings for incomplete/manual monitoring imports where auto-resolution can be risky.
- Add tests for alias matching, unmatched rows, and overwrite-sensitive import behavior.

**Success Criteria:**

- Import failures are clear and recoverable.
- Risky auto-resolution or overwrite behavior is covered by tests.
- Existing import workflows remain usable.

### Phase 6: Focused Maintainability Extraction

**Goal:** Reduce future change risk by extracting cohesive helpers from high-risk areas only where tests already protect behavior.

**Requirements:** REQ-008, REQ-015

**Scope:**

- Identify one cohesive area to extract, such as FusionSolar client helpers, performance calculations, report generation, or scheduler/job orchestration.
- Keep public behavior unchanged.
- Add tests before or during extraction.

**Success Criteria:**

- `app.py` complexity is reduced in a targeted area.
- Extracted code has a clear module boundary and test coverage.
- No unrelated refactors or feature changes are bundled into the extraction.
