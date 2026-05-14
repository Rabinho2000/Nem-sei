# Phase 4: Reporting, Performance, And Alert Confidence - Research

**Researched:** 2026-05-14  
**Domain:** Flask/SQLite production reporting, performance calculations, Telegram alert policy, PDF/XLSX exports  
**Confidence:** HIGH for local architecture and test seams; MEDIUM for third-party PDF/XLSX edge cases

## Summary

Phase 4 should be planned as a focused regression and hardening phase around existing seams in `app.py`, not as a reporting subsystem rewrite. The current code already has testable functions for report periods, production aggregation, customer production report construction, performance reference calculations, Telegram alert decisions, alert throttling, and PDF/XLSX emission. The planner should make small plans that add missing coverage first, then harden only the failure cases those tests expose.

The highest-value work is to pin date/period boundaries and operational calculations that users act on: `report_period_dates()`, `build_monitoring_report_rows()`, `build_executive_report_rows()`, `build_production_report_rows()`, `calculate_expected_production_with_diagnostic()`, `classify_performance_status()`, and `build_fusionsolar_customer_production_report()`. Existing tests cover many performance and alert basics, but period boundary tests for month/year/week/current periods, customer report failure paths, persistent/recovery alert edge cases, and PDF/XLSX failure visibility are thin.

**Primary recommendation:** split the phase into three atomic plans: performance/report period regressions, Telegram throttling/recovery regressions, and export/report failure handling regressions.

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-005 | Add focused tests for changes that affect database writes, scheduling, provider parsing, calculations, imports, exports, reports, alerts, or auth/security. | Existing pytest tests can be extended with isolated `tmp_path` SQLite DBs, direct helper calls, monkeypatched external/API/PDF boundaries, and authenticated Flask test-client route checks. |
| REQ-011 | Add regression tests around date/time, production calculations, report periods, and alert throttling. | Local seams exist for report period normalization, production aggregation, reference calculation, alert cooldowns, persistent alerts, recurrent alerts, and FusionSolar API-limit alert throttling. |
| REQ-012 | Make import/export edge cases safer, especially Excel schema drift and PDF/report generation failures. | Phase 4 should focus on export/report generation failures: `/exports` catches customer PDF failures with `flash()` and redirects; `export_rows_file()` can be tested directly for PDF/XLSX generation and monkeypatched failure cases without changing persistence. |

</phase_requirements>

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Flask | `>=3.1.3` in `requirements.txt` | Server-rendered routes, `send_file()`, flashes, test client | Existing app framework; do not add API/frontend layers. |
| sqlite3 | Python stdlib | Isolated test DBs and production persistence assertions | Matches production persistence and existing tests. |
| pytest | `>=8.0.0` in `requirements.txt` | Regression test runner | Existing project standard; official docs confirm `tmp_path` and `monkeypatch` are built-in fixtures. |
| openpyxl | `>=3.1.5` in `requirements.txt` | XLSX export/import | Existing Excel dependency; official docs use `Workbook.save()` for saving workbooks. |
| reportlab | `>=4.4.10` in `requirements.txt` | PDF export/report generation | Existing PDF dependency; official user guide documents both `pdfgen.canvas` and Platypus `SimpleDocTemplate` patterns used locally. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| requests | `>=2.32.5` | FusionSolar/Telegram HTTP integration | Only monkeypatch in Phase 4 tests; do not perform live HTTP. |
| APScheduler | `>=3.11.0` | Scheduled summaries/background jobs | Use only as surrounding context; Phase 4 should not redesign scheduling. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| pytest + monkeypatch | requests-mock/freezegun/PDF parsers | Extra dependencies are unnecessary for this phase; existing tests already control time with explicit values and monkeypatch network/PDF seams. |
| ReportLab/openpyxl | WeasyPrint/pandas/xlsxwriter | Would add dependencies and deployment risk on Raspberry Pi; not needed to harden current behavior. |
| SQLite/direct helper tests | ORM or repository abstraction | Too broad for Phase 4; use current direct SQL/helper style. |

**Installation:** none planned. Use the current `requirements.txt`.

## Architecture Patterns

### Recommended Project Structure

```text
tests/
+-- test_performance.py              # Extend production aggregation/customer report calculations
+-- test_performance_references.py   # Extend baseline/MTD/month/day reference coverage
+-- test_executive_report.py         # Extend report period and report row coverage
+-- test_telegram_alert_policy.py    # Extend cooldown, persistent, recurrent, recovery coverage
+-- test_exports.py                  # Add focused PDF/XLSX and /exports failure coverage
```

### Pattern 1: Direct Helper Regression Tests

**What:** Call pure or near-pure helpers directly with an isolated SQLite connection.  
**When to use:** Period math, performance classification, production report rows, customer report totals, alert decisions, and persistent alert processing.

**Example local pattern:**

```python
db_path = tmp_path / "performance.db"
ensure_database(str(db_path))
conn = get_db(str(db_path))
try:
    # insert minimal assets / production_records / monitoring_records
    rows = build_production_report_rows(conn, {"period": "month", "report_month": "2026-01"})
    assert rows[0]["production_kwh"] == 1000
finally:
    conn.close()
```

### Pattern 2: Route Failure Visibility Tests

**What:** Use Flask `test_client()`, authenticated session, and CSRF token only for user-visible route behavior such as `/exports` flashes and redirects.  
**When to use:** Confirm PDF/report generation failures are visible and do not mutate production data.

**Local route seam:** `/exports` posts monthly customer report options, calls `build_fusionsolar_customer_production_report()`, then `export_customer_production_pdf()`. Exceptions are flashed and redirected; FusionSolar rate-limit exceptions mark cooldown state.

### Pattern 3: Monkeypatch External/Heavy Boundaries

**What:** Monkeypatch `send_telegram_message`, FusionSolar fetch/session helpers, ReportLab document build/save methods, or `Workbook.save()` to force deterministic success/failure.  
**When to use:** Alert throttling, API-limit notification throttling, PDF/XLSX failure behavior, and "no live API call" customer report tests.

**Existing reference:** `tests/test_performance.py` monkeypatches `app.get_fusionsolar_session` to fail if local customer report data should avoid API use.

### Anti-Patterns to Avoid

- **Adding a report/export framework:** the phase needs confidence in current outputs, not a new reporting stack.
- **Testing generated PDF layout visually:** assert response metadata, non-empty bytes, selected calculations, and failure visibility; full visual validation is too expensive for this phase.
- **Changing provider production semantics without live samples:** Phase 3 explicitly left FusionSolar production key/units as an unresolved provider assumption.
- **Broad extraction from `app.py`:** only extract if a tiny helper is needed to test a behavior cleanly.
- **Committing generated PDFs/XLSX files:** generated reports are runtime artifacts and must stay out of Git.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| XLSX serialization | Manual ZIP/XML writer | `openpyxl.Workbook` | Already present and handles workbook packaging. |
| PDF table/report primitives | Custom PDF binary output | ReportLab `canvas` / `SimpleDocTemplate` / `Table` | Already present and stable for current reports. |
| Time freezing dependency | New global clock library | Explicit `date(...)`, `datetime(...)`, and optional `today_value` seams | Existing tests already use explicit dates; keeps dependencies small. |
| Alert deduplication store | New cache/table | Existing `telegram_alerts`, `alert_settings`, `app_state` | Current spam controls depend on persisted SQLite rows. |
| Background/report queue | Celery/Redis/new worker | Existing route + background job patterns | Project constraints forbid extra infrastructure at this stage. |

**Key insight:** Phase 4 risk is not missing infrastructure. It is unpinned behavior in user-facing calculations, alerts, and failure paths.

## Common Pitfalls

### Pitfall 1: Current-Month And Current-Year Report Periods Drift

**What goes wrong:** Month/year reports include future days or use full-month ranges for the current month.  
**Why it happens:** `report_period_dates()` truncates current month/year to `date.today()`, while historical months use month end.  
**How to avoid:** Add tests for `day`, default `week`, current `month`, past `month`, current `year`, and past `year`.  
**Warning signs:** Report row counts change based on system date without a test explaining why.

### Pitfall 2: Monthly Production Records Mask Daily Records

**What goes wrong:** Production report totals can differ depending on whether monthly records exist.  
**Why it happens:** `build_production_report_rows()` prefers monthly records over daily records when monthly records exist.  
**How to avoid:** Test mixed monthly/daily scenarios explicitly, including missing monthly data falling back to daily data.  
**Warning signs:** Annual/monthly reports double-count production or ignore expected/deviation values.

### Pitfall 3: Customer Report Fallback Calls FusionSolar Unexpectedly

**What goes wrong:** PDF generation may hit the live API despite complete local records, causing rate-limit or network failures.  
**Why it happens:** `build_fusionsolar_customer_production_report()` falls back to the API when local report data is incomplete or `force_api` is set.  
**How to avoid:** Keep tests that fail on unexpected API calls; add tests for no local data, disabled integration, missing mapping, and cooldown state.  
**Warning signs:** `/exports` becomes slow or rate-limited for reports that should be generated from local data.

### Pitfall 4: Alert Failures Become Spam On Recovery Loops

**What goes wrong:** Repeated failures/recoveries or persistent alerts generate duplicate Telegram messages.  
**Why it happens:** Alert keys include batch IDs, problem starts, current dates, and cooldown windows; small changes can bypass `alert_already_sent()` or `alert_recently_sent()`.  
**How to avoid:** Add tests for repeated unresolved problem scans, repeated recovery events, `RESOLVED_COOLDOWN_MINUTES`, persistent 24h/2h cooldowns, recurrent daily key behavior, and blocked rows not turning into sent rows later.  
**Warning signs:** Multiple `telegram_alerts.status = 'sent'` rows for the same asset/type/window.

### Pitfall 5: Export Exceptions Hide Or Partially Mutate State

**What goes wrong:** PDF/XLSX generation fails after state changes or presents no user-visible error.  
**Why it happens:** `export_rows_file()` returns `send_file()` directly and does not own route-level error handling; `/exports` does handle customer PDF errors.  
**How to avoid:** Test failures at route boundaries where flashes/redirects are expected, and direct helper failures where no DB write should occur.  
**Warning signs:** A failed report POST returns 500 instead of redirecting with a flash, or writes cooldown/job/state unexpectedly for non-rate-limit failures.

## Code Examples

Verified local patterns to reuse in plans:

### Report Period Boundary Test

```python
from app import report_period_dates

def test_past_month_report_uses_full_month() -> None:
    assert report_period_dates("month", report_month="2026-01") == ("2026-01-01", "2026-01-31")
```

If testing current month/year, monkeypatch `app.date` only if necessary; prefer direct helpers with explicit `report_month`/`report_year` where possible.

### Alert Cooldown Test

```python
now = datetime(2026, 5, 14, 10, 0, 0)
record_telegram_alert(conn, asset_id, "resolvido", "old-key", "msg", "sent", sent_at=now)
set_alert_setting(conn, "RESOLVED_COOLDOWN_MINUTES", "60")

allowed, reason = alert_decision(conn, asset, "resolvido", "new-key", now + timedelta(minutes=30))
assert not allowed
assert reason == "cooldown"
```

### Export Failure Route Test

```python
monkeypatch.setattr(app_module, "build_fusionsolar_customer_production_report", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("boom")))
response = client.post("/exports", data={"asset_id": str(asset_id), "report_month": "2026-01", "csrf_token": token})
assert response.status_code == 302
```

Then assert the flashed error by following redirects or inspecting session flashes, and assert no unrelated DB rows changed.

## State Of The Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Live API report generation by default | Local production records first, FusionSolar fallback only when needed | Current local code | Tests should protect local-first behavior. |
| Direct alert send only | Alert decision + persisted `telegram_alerts` sent/blocked/failed rows | Current local code | Spam control depends on DB assertions, not just mocked send calls. |
| Generic export templates as primary UI | `/exports` currently exposes monthly customer production PDF only | Current local code | Phase 4 should not assume generic export UI exists. |
| Provider behavior inferred from code | Phase 3 fixture-backed provider contracts plus assumption note | Completed 2026-05-14 | Phase 4 can consume selected production values but should not redefine provider contracts. |

**Deprecated/outdated:** none identified in local code for this phase. The risk is incomplete coverage and route-level hardening, not use of obsolete APIs.

## Open Questions

1. **Should generic `export_rows_file()` be wired to UI in Phase 4?**
   - What we know: `build_export_dataset()` and predefined export templates exist, but `rg` found no route calling `export_rows_file()`.
   - What's unclear: Whether generic export UI was intentionally removed/deferred or is incomplete.
   - Recommendation: Do not build generic export UI in Phase 4 unless planning confirms it is required; test helpers directly if touched.

2. **What is the exact business rule for week reports?**
   - What we know: fallback period returns `today - 7 days` through `today`.
   - What's unclear: Whether "Semanal" should mean last 7 days inclusive, calendar week, or previous closed week.
   - Recommendation: Pin current behavior in tests before changing it.

3. **Should customer PDF failures record audit rows?**
   - What we know: `/exports` currently uses flash/logging, not an export audit table.
   - What's unclear: Whether the operator needs historical failed-report audit.
   - Recommendation: For Phase 4, require visible failure and no data corruption; defer audit history unless explicitly requested.

4. **How far should PDF/XLSX validation go?**
   - What we know: ReportLab/openpyxl generation can be smoke-tested in memory.
   - What's unclear: Whether visual layout correctness is a requirement.
   - Recommendation: Use smoke and calculation tests only; visual PDF review remains manual.

## Validation Notes

Existing focused command run during research:

```text
python -m pytest -q tests/test_performance.py tests/test_performance_references.py tests/test_performance_backfill.py tests/test_performance_debug.py tests/test_executive_report.py tests/test_telegram_alert_policy.py
```

Result:

```text
56 passed in 5.03s
```

Recommended Phase 4 quick gate:

```text
python -m pytest -q tests/test_performance.py tests/test_performance_references.py tests/test_performance_backfill.py tests/test_performance_debug.py tests/test_executive_report.py tests/test_telegram_alert_policy.py tests/test_exports.py
```

Recommended Wave 0 gaps:

- `tests/test_exports.py` does not exist; create it before export hardening.
- Add period-boundary tests to `tests/test_executive_report.py` or a new focused report-period test module.
- Add persistent/recovery alert edge tests to `tests/test_telegram_alert_policy.py`.
- Add customer report missing-data/cooldown/route-failure tests to `tests/test_performance.py` or `tests/test_exports.py`.

## Sources

### Primary (HIGH confidence)

- Local requirements and roadmap: `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `.planning/PROJECT.md`, `.planning/STATE.md`.
- Local codebase maps: `.planning/codebase/STACK.md`, `.planning/codebase/ARCHITECTURE.md`, `.planning/codebase/CONCERNS.md`, `.planning/codebase/CONVENTIONS.md`, `.planning/codebase/TESTING.md`, `.planning/codebase/INTEGRATIONS.md`.
- Phase 3 boundary: `.planning/phases/03-provider-integration-correctness/03-VERIFICATION.md`.
- Local implementation seams: `app.py`, `templates/exports.html`, `requirements.txt`.
- Local tests: `tests/test_performance.py`, `tests/test_performance_references.py`, `tests/test_performance_backfill.py`, `tests/test_performance_debug.py`, `tests/test_executive_report.py`, `tests/test_telegram_alert_policy.py`.

### Primary external (HIGH-MEDIUM confidence)

- pytest official documentation: built-in `tmp_path` and `monkeypatch` fixtures.
- openpyxl official documentation: `Workbook.save()` is the standard workbook save path.
- ReportLab official user guide: documents `pdfgen.canvas`, Platypus `SimpleDocTemplate`, `Paragraph`, `Spacer`, and table-based PDF construction.

### Tertiary (LOW confidence)

- None used for implementation recommendations.

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH - verified from `requirements.txt`, local code imports, and official docs for test/export primitives.
- Architecture: HIGH - verified from local maps and direct code inspection.
- Test seams: HIGH - focused suite passed during research.
- Export failure edge cases: MEDIUM - route behavior is visible locally, but generic export helpers are not route-wired and failure expectations need planning confirmation.
- Provider production semantics: MEDIUM - Phase 3 pinned local behavior but explicitly left live provider unit/key meaning as a human/provider check.

**Research date:** 2026-05-14  
**Valid until:** 2026-06-13 for local architecture; revisit sooner if `/exports`, `production_records`, or Telegram alert policy changes before planning.
