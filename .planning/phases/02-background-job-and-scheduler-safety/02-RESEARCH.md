# Phase 2: Background Job And Scheduler Safety - Research

**Researched:** 2026-05-14
**Domain:** Flask in-process APScheduler, SQLite-backed background jobs, provider sync dispatch
**Confidence:** HIGH

## Summary

This phase should harden the scheduler and persisted background job paths without changing the deployment shape. The project already uses the correct simple architecture for its constraints: one Flask/Gunicorn worker process, APScheduler `BackgroundScheduler` in-process, SQLite for durable job rows, and explicit startup recovery. The planner should keep that shape and add narrow fixes plus tests around determinism.

The main implementation risks are not library choice. They are duplicate scheduler startup, ambiguous provider dispatch, incomplete recovery of pending/stale background jobs, and silent scheduled job failures. Existing code already has useful seams in `start_integration_scheduler()`, `refresh_integration_scheduler()`, `schedule_background_job()`, `schedule_pending_background_jobs()`, `run_scheduled_integration_sync()`, and the `background_jobs` helper functions.

**Primary recommendation:** Keep APScheduler in-process and SQLite-backed job rows, then add focused provider-neutral scheduler helpers/tests that verify one-process registration, duplicate prevention, startup recovery, and FusionSolar/Sigenergy scheduled dispatch.

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-004 | Keep scheduled jobs registered once per process and preserve the documented one-worker deployment requirement. | Use the existing module-global `SCHEDULER`, stable job IDs, `replace_existing=True`, and one-worker docs as invariants; add tests around repeated startup/refresh behavior. |
| REQ-005 | Add focused tests for changes that affect database writes, scheduling, provider parsing, calculations, imports, exports, reports, alerts, or auth/security. | Extend the existing pytest style with isolated `tmp_path` databases and monkeypatched scheduler/provider functions. |
| REQ-009 | Improve observability of background jobs, sync runs, and failed scheduled work without adding new services. | Persist background job failures in `background_jobs`, integration failures in `integration_sync_runs`/`integration_configs`, and add deterministic logs/status updates for recovery and scheduler failures. |

</phase_requirements>

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Flask | `>=3.1.3` | App factory, request context, app context for scheduled jobs | Existing runtime and tests already depend on Flask context patterns. |
| SQLite `sqlite3` | Python stdlib | Durable job/sync state | Existing persistence layer; no new infrastructure allowed. |
| APScheduler | `>=3.11.0` | In-process cron/date scheduling | Already installed; official docs support stable IDs, `replace_existing`, `max_instances`, `coalesce`, and scheduler events. |
| Gunicorn | `>=23.0.0` | Production WSGI process | Current Docker/Compose command uses `-w 1 --threads 4`, matching the single-process scheduler requirement. |
| pytest | `>=8.0.0` | Regression tests | Existing test suite pattern. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `monitoring_board.db.get_db` | local helper | SQLite connections with PRAGMAs | Use for scheduler/background-job connections, including tests. |
| `monitoring_board.services.fusionsolar.normalize_sync_hours` | local helper | Normalize configured sync hours | Use for cron registration input; do not add another parser. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| In-process APScheduler | Celery/RQ/external worker | Out of scope and violates REQ-001/REQ-018. |
| SQLite persisted job rows | APScheduler persistent job store | APScheduler docs warn against sharing job stores across processes; current DB rows provide app-specific recovery and UI observability. |
| One Gunicorn worker | Multiple workers with locks | Would require scheduler redesign; preserve one worker for this phase. |

**Installation:** No new dependencies should be added.

## Architecture Patterns

### Recommended Project Structure

```text
app.py
|-- scheduler globals and lifecycle helpers
|-- background_jobs table helpers
|-- scheduled provider dispatch helpers
`-- focused tests under tests/test_scheduler_safety.py or nearby existing files
```

Keep changes near the existing functions unless a tiny extraction removes real duplication. Do not start Phase 6-style broad modularization here.

### Pattern 1: Single In-Process Scheduler

**What:** `SCHEDULER` is module-global, created once in `start_integration_scheduler()`, and guarded by `if SCHEDULER is not None: return`.

**When to use:** Keep this as the process-local duplicate prevention boundary. Tests should reset or monkeypatch `app_module.SCHEDULER` carefully.

**Example:**

```python
def start_integration_scheduler(app: Flask) -> None:
    global SCHEDULER
    if SCHEDULER is not None:
        return
    SCHEDULER = BackgroundScheduler(timezone="Europe/Lisbon")
    SCHEDULER.start()
    refresh_integration_scheduler(app)
    schedule_pending_background_jobs(app)
```

### Pattern 2: Stable Scheduler IDs With Replacement

**What:** Scheduled sync jobs use deterministic IDs like `integration-sync-fusionsolar-1`, and one-shot jobs use `background-job-{job_id}`.

**When to use:** Continue using explicit IDs and `replace_existing=True`. Add assertions that repeated refreshes do not multiply jobs.

**Official support:** APScheduler `add_job()` accepts explicit `id`, `replace_existing`, `misfire_grace_time`, `coalesce`, and `max_instances`.

### Pattern 3: SQLite Is The Durable Job State

**What:** `background_jobs` owns durable state: `pending`, `running`, `success`, `failed`; APScheduler is only the execution trigger.

**When to use:** Startup recovery should query SQLite, mark stale running jobs failed, then schedule pending jobs. Runtime failure should persist `error_message` and `finished_at`.

### Pattern 4: Provider-Neutral Dispatch

**What:** `run_fusionsolar_sync()` is actually a generic sync orchestrator because it calls `run_provider_check()`, which dispatches Sigenergy when `provider == INTEGRATION_PROVIDER_SIGENERGY`.

**When to use:** For Phase 2, prefer introducing/using a provider-neutral alias such as `run_integration_sync()` or at minimum update scheduled dispatch/tests so Sigenergy is explicitly covered. Avoid changing provider parsing internals.

### Anti-Patterns to Avoid

- **Adding Celery/Redis/Postgres:** violates project constraints and solves a larger problem than this phase.
- **Making APScheduler persistent job stores the source of truth:** current app-specific rows are easier to inspect and recover; APScheduler docs warn that shared stores across processes cause incorrect behavior.
- **Relying on manual UI checks for scheduler safety:** use fake scheduler objects and `tmp_path` SQLite tests.
- **Broad renames through `app.py`:** provider-neutral naming is useful, but keep edits scoped to scheduler/dispatch seams.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Cron parsing | Custom hour/minute parser | Existing `normalize_sync_hours()` plus APScheduler cron trigger | Existing helper is tested and handles project format. |
| Duplicate scheduled jobs | In-memory lists or ad hoc flags | APScheduler IDs with `replace_existing=True` and `max_instances=1` where relevant | APScheduler already supports this. |
| Durable queue engine | New queue table/service | Existing `background_jobs` table | Already tied to UI, tests, and recovery. |
| Multi-process coordination | SQLite advisory lock scheme | One documented Gunicorn worker | APScheduler lacks interprocess signalling for shared job stores. |
| Full scheduler observability stack | Metrics service/dashboard | Existing logs plus `background_jobs`, `integration_sync_runs`, `integration_configs.last_error` | Matches REQ-009 without new services. |

**Key insight:** The hard part is not scheduling a function. It is making every trigger path idempotent and visible after restarts while staying inside one process.

## Common Pitfalls

### Pitfall 1: Multiple Workers Duplicate Jobs

**What goes wrong:** Each worker imports `app.py`, creates its own scheduler, and runs the same cron jobs.

**Why it happens:** APScheduler state is process-local; Gunicorn workers do not share Python globals.

**How to avoid:** Preserve `gunicorn -w 1 --threads 4`; add comments/tests that assume process-local idempotency only.

**Warning signs:** Deployment docs or Compose command start using `-w 2+`, `WEB_CONCURRENCY`, or multiple app containers.

### Pitfall 2: Scheduled Sigenergy Path Is Untested

**What goes wrong:** Scheduled jobs pass provider names, but the scheduled function currently calls `run_fusionsolar_sync(conn, provider, trigger_type="scheduled")`. This works only because that function delegates to `run_provider_check()`, but the name and tests are FusionSolar-biased.

**Why it happens:** Sigenergy was added after FusionSolar and reused generic sync internals.

**How to avoid:** Add tests that scheduled Sigenergy dispatch calls the provider-neutral sync path with `trigger_type="scheduled"` and records provider-specific failures.

**Warning signs:** Tests monkeypatch only `run_fusionsolar_check`; no test proves `INTEGRATION_PROVIDER_SIGENERGY` scheduled behavior.

### Pitfall 3: Pending Job Recovery Is Partial

**What goes wrong:** `schedule_pending_background_jobs()` only schedules the first 10 pending jobs. Older pending rows beyond that limit can stay pending after restart.

**Why it happens:** Query has `LIMIT 10`.

**How to avoid:** Planner should decide whether to remove the limit, loop in bounded batches, or document the cap. For deterministic recovery, tests should cover more than 10 pending rows or explicitly assert the intended cap.

**Warning signs:** Users see pending jobs that never get a `started_at` after restart.

### Pitfall 4: Running Jobs Can Stall Without A Restart

**What goes wrong:** `mark_stale_running_background_jobs_failed()` only runs during startup recovery. If the scheduler thread stays alive but a job hangs or the process survives an exception path not captured by `run_background_job()`, stale rows can remain `running`.

**Why it happens:** No heartbeat or periodic recovery job exists.

**How to avoid:** For this phase, at least make startup recovery deterministic and visible. A lightweight periodic stale-job sweep is possible, but only add it if scoped and tested.

**Warning signs:** `background_jobs.status='running'` with old `started_at`.

### Pitfall 5: Misfires And Overlaps Are Implicit

**What goes wrong:** Cron jobs delayed by downtime/thread pressure can run late or repeatedly; long provider syncs can overlap the next schedule.

**Why it happens:** Current cron registrations do not set explicit `coalesce`, `misfire_grace_time`, or `max_instances`.

**How to avoid:** Set explicit APScheduler options for scheduled provider jobs, likely `max_instances=1` and `coalesce=True`; choose a conservative `misfire_grace_time` or document default behavior.

**Warning signs:** Back-to-back scheduled sync runs after downtime, or overlapping provider API calls.

### Pitfall 6: Tests Import `app.py` And Start Global Scheduler

**What goes wrong:** Tests that import `app` may leave a running APScheduler around, creating flaky global state.

**Why it happens:** `app = create_app()` runs at module import and `create_app()` calls `start_integration_scheduler(app)`.

**How to avoid:** Scheduler tests should monkeypatch `BackgroundScheduler` or reset/shutdown `app_module.SCHEDULER` in `finally` blocks. Prefer fake scheduler objects for registration tests.

## Code Examples

Verified local patterns:

### Background Job State Transition

```python
def mark_background_job_running(conn: sqlite3.Connection, job_id: int) -> bool:
    cursor = conn.execute(
        """
        UPDATE background_jobs
        SET status = 'running', started_at = ?, error_message = NULL
        WHERE id = ? AND status = 'pending'
        """,
        (datetime.now().isoformat(timespec="seconds"), job_id),
    )
    conn.commit()
    return cursor.rowcount == 1
```

### One-Shot Scheduler Registration

```python
SCHEDULER.add_job(
    func=run_background_job,
    trigger="date",
    run_date=datetime.now(),
    args=[app, job_id],
    id=f"background-job-{job_id}",
    replace_existing=True,
    max_instances=1,
)
```

### Provider Dispatch Seam

```python
def run_provider_check(conn: sqlite3.Connection, provider: str, dry_run: bool = False) -> dict[str, Any]:
    if provider == INTEGRATION_PROVIDER_SIGENERGY:
        return run_sigenergy_check(conn, provider, dry_run=dry_run)
    return run_fusionsolar_check(conn, provider, dry_run=dry_run)
```

Recommended test shape:

```python
def test_refresh_scheduler_registers_one_job_per_provider_hour(tmp_path, monkeypatch):
    # Use ensure_database(tmp_path / "scheduler.db"), insert enabled provider configs,
    # replace app_module.SCHEDULER with a fake object collecting add_job/remove_job calls,
    # then call refresh_integration_scheduler(flask_app) twice and assert stable IDs.
```

## State Of The Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Blind in-process cron per worker | One documented Gunicorn worker with in-process APScheduler | Existing project decision, reinforced in Phase 1 | Correct for this app; scale-out would need redesign. |
| Synchronous heavy request work | Persist `background_jobs` and trigger date jobs | Existing performance routes | Keeps requests responsive and makes job state inspectable. |
| FusionSolar-only sync assumptions | Generic provider list with FusionSolar and Sigenergy configs | Existing code before Phase 2 | Scheduler tests must cover both providers. |

**Deprecated/outdated for this project:**

- Adding external queue infrastructure for this phase: out of scope.
- Assuming APScheduler can coordinate one persistent job store across worker processes: contradicted by APScheduler FAQ.
- Treating `run_fusionsolar_sync()` as provider-specific despite generic internals: creates planning/test ambiguity.

## Open Questions

1. **Should pending recovery schedule all rows or keep a cap?**
   - What we know: current code schedules `LIMIT 10`.
   - What's unclear: whether this was intentional throttling or an accidental cap.
   - Recommendation: plan a deterministic behavior and test it. Prefer all pending rows unless resource concerns require a documented cap.

2. **Should stale running recovery also run periodically?**
   - What we know: startup recovery exists and is tested at helper level.
   - What's unclear: whether runtime stalls are common enough to justify a periodic sweeper.
   - Recommendation: keep Phase 2 focused on startup recovery unless adding a small scheduled sweep is low-risk and covered.

3. **How much renaming is acceptable around `run_fusionsolar_sync()`?**
   - What we know: function is generic enough for Sigenergy through `run_provider_check()`.
   - What's unclear: whether a broad rename would touch too much of `app.py`.
   - Recommendation: add provider-neutral wrapper/tests first; defer broad rename unless it stays small.

## Sources

### Primary (HIGH confidence)

- Local codebase: `app.py`, especially scheduler/job functions around `SCHEDULER`, `start_integration_scheduler()`, `refresh_integration_scheduler()`, `schedule_pending_background_jobs()`, `run_background_job()`, `run_provider_check()`, and `run_fusionsolar_sync()`.
- Local planning docs: `.planning/REQUIREMENTS.md`, `.planning/STATE.md`, `.planning/ROADMAP.md`, `.planning/codebase/*.md`.
- APScheduler 3.11.2.post1 user guide: https://apscheduler.readthedocs.io/en/3.x/userguide.html
- APScheduler base scheduler API: https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/base.html
- APScheduler FAQ on worker/process job store sharing: https://apscheduler.readthedocs.io/en/3.x/faq.html
- Gunicorn FAQ/process model reference: https://docs.gunicorn.org/en/20.0.4/faq.html

### Secondary (MEDIUM confidence)

- Existing tests: `tests/test_db_helpers.py`, `tests/test_performance_debug.py`, `tests/test_fusionsolar_sync.py`.
- Deployment docs: `Dockerfile`, `docker-compose.yml`, `README.md`, `docs/raspberry-pi-deployment.md`.

### Tertiary (LOW confidence)

- None used for final recommendations.

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH - versions and dependencies are in `requirements.txt`; no new library is needed.
- Architecture: HIGH - existing code and planning docs clearly define in-process scheduler plus SQLite job rows.
- Pitfalls: HIGH - most are directly visible in local code and cross-checked against APScheduler official docs.
- Provider dispatch: MEDIUM - code path is clear, but Sigenergy scheduled behavior lacks existing tests.

**Research date:** 2026-05-14
**Valid until:** 2026-06-13 for library behavior; local findings remain valid until scheduler/background job code changes.
