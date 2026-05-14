# Phase 3: Provider Integration Correctness - Research

**Researched:** 2026-05-14  
**Domain:** FusionSolar and Sigenergy provider parsing, timestamps, units, and sync compatibility  
**Confidence:** MEDIUM

## Summary

Phase 3 should be planned as a regression-test and documentation phase, not a behavior rewrite. The current implementation already has workable seams for provider correctness: FusionSolar URL/status helpers in `monitoring_board/services/fusionsolar.py`, provider HTTP/fetch/normalization helpers in `app.py`, and existing pytest modules for FusionSolar sync, production selection, backfill, rate-limit, and session-expiry behavior.

The high-risk areas are not missing libraries; they are unpinned provider contracts. FusionSolar behavior depends on response shapes for station lists, realtime KPI, alarm rows, daily/monthly KPI rows, `failCode` handling, `XSRF-TOKEN`, `collectTime`, and production keys. Sigenergy behavior depends on token response parsing, optional JSON-string `data`, system list shape variants, realtime status fields, energy-flow fields, and API rate limits. The planner should create small tasks that add realistic fixture files or fixture builders, then assert current behavior against them before any behavior changes are considered.

**Primary recommendation:** Add fixture-backed tests around the existing parser/sync seams first, then document unresolved assumptions in comments or planning notes without changing provider behavior unless a test exposes a clear correctness bug.

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-005 | Add focused tests for changes that affect provider parsing, calculations, and scheduling/database writes. | Existing pytest structure supports narrow tests in `tests/test_fusionsolar_sync.py`, `tests/test_performance.py`, `tests/test_performance_backfill.py`, and a new Sigenergy-focused module. |
| REQ-006 | Treat FusionSolar and Sigenergy fields, pagination, rate limits, timestamps, and units as assumptions requiring verification. | Research identifies exact code paths and external-doc assumptions to pin with fixtures: FusionSolar `data.list`, `dataItemMap`, `collectTime`, `failCode`; Sigenergy JSON-string `data`, status fields, energy-flow fields, and request-rate constraints. |
| REQ-011 | Add regression tests around date/time and production calculations. | FusionSolar production date parsing and production-key selection are central to this phase: `parse_fusionsolar_collect_date()`, `select_production_value()`, `store_production_kpi_record()`, and backfill month/day filtering. |

</phase_requirements>

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `pytest` | `>=8.0.0` in `requirements.txt` | Regression tests | Existing project test runner; no new test framework needed. |
| `sqlite3` stdlib | Python 3.12 runtime | Integration sync persistence assertions | Matches production persistence and existing tests. |
| `requests` | `>=2.32.5` | Provider HTTP boundaries | Existing provider clients use direct `requests`; tests should monkeypatch functions or fake responses. |
| JSON fixture files or local fixture builders | stdlib `json` | Pin provider response examples | Keeps response-shape assumptions reviewable without real API calls. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `monkeypatch` pytest fixture | pytest built-in | Replace network/session functions | Use for provider sync, rate-limit, and session-expiry scenarios. |
| `tmp_path` pytest fixture | pytest built-in | Isolated SQLite databases | Use when asserting sync writes, unresolved rows, production records, or run history. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Plain fixture builders / JSON files | `responses`, `requests-mock`, VCR-style cassettes | Extra dependency is unnecessary because most code already exposes fetch/parse seams. Use only if future tests must verify raw HTTP request details. |
| Current `app.py` helper tests | Large provider-client extraction | Extraction may be useful later, but Phase 3 can reduce risk with tests first and preserve behavior. |

**Installation:** No new dependency recommended.

## Architecture Patterns

### Recommended Project Structure

```text
tests/
+-- fixtures/
|   +-- fusionsolar/
|   |   +-- stations_page_1.json
|   |   +-- realtime_kpi.json
|   |   +-- alarms_active.json
|   |   +-- kpi_day_rows.json
|   |   +-- kpi_month_rows.json
|   +-- sigenergy/
|       +-- auth_success_json_string.json
|       +-- systems_list.json
|       +-- realtime_data.json
|       +-- energy_flow.json
+-- test_provider_fixtures.py
+-- test_fusionsolar_sync.py
+-- test_performance.py
+-- test_performance_backfill.py
+-- test_sigenergy_sync.py
```

Do not create the full tree unless the plan uses it. For a small phase, fixture builders inside one or two test modules are acceptable, but JSON files are better when the goal is to pin realistic external contracts.

### Pattern 1: Test Pure Normalizers First

**What:** Exercise response-shape parsing without database or scheduler involvement.  
**When to use:** Status mapping, row normalization, timestamp parsing, production key selection, token response parsing.

Relevant seams:
- `monitoring_board/services/fusionsolar.py:31` maps FusionSolar status codes and text statuses.
- `app.py:8424` normalizes FusionSolar station/realtime/alarm data into local provider rows.
- `app.py:7436` parses FusionSolar KPI dates from timestamp/date fields.
- `app.py:7593` selects production values from `dataItemMap`.
- `app.py:8518` through `app.py:8636` parses Sigenergy token/system/realtime/energy-flow data.

### Pattern 2: Test Sync Orchestration With Mocked Provider Checks

**What:** Use temporary SQLite databases, `ensure_database()`, and monkeypatched provider fetch/check functions to verify rows written and compatibility with manual/scheduled trigger paths.  
**When to use:** Matched/unresolved provider rows, sync run records, monitoring records, config error persistence.

Existing reference: `tests/test_scheduler_safety.py` verifies scheduled provider dispatch goes through `run_integration_sync()` and persists provider failures. `tests/test_fusionsolar_sync.py` verifies same-day recovery behavior.

### Pattern 3: Treat Production KPI Sync As Contract + Calculation

**What:** Test API row shape, selected production key, period date, stored raw key/value, and `production_records` side effects together.  
**When to use:** FusionSolar day/month KPI handling and backfill.

Existing reference: `tests/test_performance.py` covers production key priority; `tests/test_performance_backfill.py` covers month-batched day backfill, rate-limit wait, and session-expiry retry.

### Anti-Patterns to Avoid

- **Live provider calls in tests:** Phase success should not depend on FusionSolar/Sigenergy availability, account permissions, or rate limits.
- **Broad `app.py` refactor before tests:** The phase goal is correctness confidence. Extraction belongs only if needed to make a specific testable seam.
- **Synthetic-only fixtures with invented keys:** Fixtures must look like real provider packets and include raw payload fields the app stores.
- **Silent unit assumptions:** Tests and notes should say whether a value is kWh, kW, milliseconds, seconds, UTC, local, or provider-local when known.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HTTP mocking framework | Custom fake transport layer | Monkeypatch existing fetch/check/session functions | Existing code already separates fetch and normalize enough for this phase. |
| Time freezing framework | Global time monkeypatching | Explicit `date(...)`, `datetime(...)`, `today_value`, `now_value` parameters already present | Existing tests already follow explicit-date patterns. |
| New provider abstraction | Generic provider SDK layer | Current `run_provider_check()` / `run_integration_sync()` seams | Avoids large architecture changes in `app.py`. |
| Rate-limit scheduler | New queue/cooldown service | Existing `mark_fusionsolar_performance_rate_limited()` and persisted `app_state` cooldown | Keeps Raspberry Pi/simple SQLite constraints intact. |

**Key insight:** The complex part is provider contract drift, not application infrastructure. Preserve the current direct-call shape and add contract-like mocked examples around it.

## Common Pitfalls

### Pitfall 1: FusionSolar Timestamp Timezone Drift

**What goes wrong:** `parse_fusionsolar_collect_date()` uses `datetime.fromtimestamp()`, which converts epoch timestamps using the host local timezone. If FusionSolar `collectTime` is UTC or plant-local, dates can shift around midnight or DST transitions.

**Why it happens:** The current code treats millisecond and second timestamps as local timestamps. The official and secondary references identify `collectTime` as milliseconds, but the precise timezone semantics still need confirmation against actual account examples.

**How to avoid:** Plan tests with explicit millisecond timestamps around Europe/Lisbon midnight/DST boundaries and document the assumption. Do not change conversion behavior unless the fixture proves a bug and compatibility impact is understood.

**Warning signs:** Same provider row stored under previous/next local date; daily backfill ignores rows because `row_date.replace(day=1) != month_value`.

### Pitfall 2: FusionSolar Production Key Ambiguity

**What goes wrong:** Production can be selected from `PVYield`, `inverterYield`, or legacy `inverter_power`, and these may represent different concepts depending on API version/device type.

**Why it happens:** Current code prioritizes `PVYield`, then `inverterYield`, then `inverter_power`. Huawei docs say recent SmartPVMS API versions added `PVYield` and `inverterYield` to daily/monthly/yearly plant packets; older examples use `inverter_power`.

**How to avoid:** Pin fixtures covering all three key variants and assert `selected_production_key`, `selected_production_raw_value`, and `production_kwh` are stored. Document that the app treats selected values as kWh.

**Warning signs:** `production_kwh` is missing while `dataItemMap` contains other energy-like keys; reports use `0`/missing despite payload having usable production fields.

### Pitfall 3: String-Based FusionSolar Error Handling

**What goes wrong:** Rate limit and session expiry are detected by searching exception strings for `failCode=407`, `error code 407`, `codigo 407`, `failCode=305`, or `USER_MUST_RELOGIN`.

**Why it happens:** `post_fusionsolar_json()` raises `ValueError` with message text rather than a structured provider error object.

**How to avoid:** Add tests for error payloads flowing through `post_fusionsolar_json()` and existing handlers, including `failCode` as int/string and message variants. Do not plan a structured error refactor unless tests show current behavior misses known payloads.

**Warning signs:** Cooldown is not persisted on rate-limit responses; expired sessions are not retried.

### Pitfall 4: Sigenergy `data` May Be A JSON String

**What goes wrong:** Treating Sigenergy `data` as an object only would break token or data parsing when the API returns JSON-encoded strings.

**Why it happens:** Observed Sigenergy examples show auth `data` as a JSON string containing `accessToken` and `expiresIn`. Current code has `parse_provider_payload_data()` for this, but coverage should pin it.

**How to avoid:** Add fixture tests for both string and object `data` forms for auth, system list, realtime, and energy-flow endpoints.

**Warning signs:** Login succeeds at HTTP level but fails with "data JSON valido" or "accessToken" missing.

### Pitfall 5: Sigenergy Rate Limits Are Easier To Exceed Than Current Schedule Suggests

**What goes wrong:** Fetching system list, realtime, and energy flow for many systems can exceed provider request limits or freshness constraints.

**Why it happens:** Secondary Sigenergy API notes describe energy-flow/realtime data as once per five minutes per station and general API calls as limited for third-party accounts. Current sync sleeps `0.2` seconds per system and does not have a Sigenergy cooldown strategy.

**How to avoid:** Phase 3 should document this as an assumption and test existing error persistence paths, not add a new scheduler. If behavior changes are later needed, plan them separately.

**Warning signs:** Scheduled Sigenergy syncs repeatedly fail with provider limit messages; app records `last_sync_status='error'` but no specific cooldown.

## Code Examples

Verified patterns from existing code:

### Fixture-Backed Production Key Assertion

```python
def test_fusionsolar_fixture_selects_pvyield_and_stores_raw_key(conn):
    kpi_row = load_json("tests/fixtures/fusionsolar/kpi_day_rows.json")["data"][0]
    production_kwh, selected_key, raw_value = app_module.select_production_value(kpi_row["dataItemMap"])

    assert production_kwh == 123.45
    assert selected_key == "PVYield"
    assert raw_value == "123.45"
```

### Provider Sync Assertion With Mocked Check

```python
def test_sigenergy_sync_persists_unresolved_fixture_row(conn, monkeypatch):
    fixture_row = app_module.normalize_sigenergy_system_row(
        {"systemId": "SYS-1", "systemName": "Central Sig"},
        {"systemStatus": "running"},
        {"pvPower": 4.2, "batterySoc": 80},
    )

    monkeypatch.setattr(
        app_module,
        "run_provider_check",
        lambda _conn, _provider, dry_run=False: {
            "rows": [fixture_row],
            "station_count": 1,
            "realtime_count": 1,
            "alarm_count": 0,
            "alarm_error": "",
        },
    )

    result = app_module.run_integration_sync(conn, app_module.INTEGRATION_PROVIDER_SIGENERGY)

    assert result["unresolved"] == 1
```

Use these as planning patterns, not exact copy-paste requirements; fixture shape should match actual files created in the implementation phase.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| FusionSolar plant KPI examples using mostly `inverter_power` | SmartPVMS 24.5.0 docs add `PVYield` and `inverterYield` to hourly/daily/monthly/yearly plant packets | Huawei SmartPVMS 24.5.0 reference | Current priority order `PVYield`, `inverterYield`, `inverter_power` is plausible but must stay documented/tested. |
| One provider-specific scheduled path | Provider-neutral scheduled wrapper via `run_integration_sync()` | Phase 2 in this project | Phase 3 tests should ensure manual and scheduled compatibility remains provider-neutral. |
| Ad hoc Sigenergy shape handling | Flexible parser supporting object/list/string data variants | Existing app code | Needs fixture-backed tests because official public docs are limited/inaccessible. |

**Deprecated/outdated:**
- Planning new behavior around `inverter_power` alone is outdated for recent FusionSolar packets.
- Planning Sigenergy as a FusionSolar-like API is unsafe; token format, headers, status fields, and rate limits differ.

## Open Questions

1. **What is the authoritative timezone for FusionSolar `collectTime`?**
   - What we know: Current code accepts milliseconds/seconds and uses local `datetime.fromtimestamp()`. Secondary docs and examples show millisecond timestamps.
   - What's unclear: Whether timestamps should be interpreted as UTC, management-system timezone, plant timezone, or caller-local timezone.
   - Recommendation: Plan a fixture test documenting current behavior and add a planning note that real account samples are needed before changing conversion.

2. **Are `PVYield` and `inverterYield` always kWh for the project's target FusionSolar endpoint/account?**
   - What we know: Huawei docs describe PV yield and inverter yield as kWh, and SmartPVMS 24.5.0 says those fields were added to plant KPI packets.
   - What's unclear: Which key best matches customer-facing "production" for every installed plant configuration.
   - Recommendation: Keep current priority order but make it explicit in tests and notes.

3. **What exact Sigenergy error codes mean token expiry or rate limiting?**
   - What we know: Current code raises on non-zero `code`; secondary notes mention rate limits but not stable error-code contracts.
   - What's unclear: Exact `code`/HTTP status patterns for throttling and expired tokens.
   - Recommendation: Plan tests only for current generic error persistence, and document Sigenergy throttle/token-expiry codes as assumptions needing real provider samples.

4. **Where should realistic fixtures live?**
   - What we know: Current tests use module-local builders, not shared fixtures.
   - What's unclear: Whether repository preference is JSON files or builders.
   - Recommendation: Use JSON fixture files only for external response packets; keep DB setup helpers local to tests.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest `>=8.0.0` |
| Config file | none |
| Quick run command | `python -m pytest -q tests/test_fusionsolar_service.py tests/test_fusionsolar_sync.py tests/test_performance.py tests/test_performance_backfill.py tests/test_scheduler_safety.py` |
| Full suite command | `python -m pytest -q` |
| Estimated runtime | Unknown until run locally; existing targeted tests should be suitable for per-task feedback. |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| REQ-005 | Provider parsing and sync changes are covered by focused tests. | unit/integration | `python -m pytest -q tests/test_fusionsolar_sync.py tests/test_sigenergy_sync.py` | partial; Sigenergy test module is a Wave 0 gap |
| REQ-006 | FusionSolar/Sigenergy response shapes, rate limits, timestamps, and units are explicit. | contract-style unit tests with fixtures | `python -m pytest -q tests/test_provider_fixtures.py tests/test_performance_backfill.py` | fixture file/module is a Wave 0 gap |
| REQ-011 | Production date and calculation assumptions are regression tested. | unit/integration | `python -m pytest -q tests/test_performance.py tests/test_performance_backfill.py` | partial |

### Nyquist Sampling Rate

- **Minimum sample interval:** After every committed task, run the targeted command for the touched provider area.
- **Full suite trigger:** Before completing the phase.
- **Phase-complete gate:** `python -m pytest -q` green before verification.
- **Estimated feedback latency per task:** Use targeted provider tests first; run full suite at phase end.

### Wave 0 Gaps

- [ ] `tests/fixtures/fusionsolar/*.json` or local fixture builders with realistic station/realtime/alarm/KPI packets.
- [ ] `tests/fixtures/sigenergy/*.json` or local fixture builders with auth/system/realtime/energy-flow packets.
- [ ] `tests/test_sigenergy_sync.py` for Sigenergy parser and generic sync behavior.
- [ ] `tests/test_provider_fixtures.py` only if fixture validation is separated from provider-specific tests.

## Sources

### Primary (HIGH confidence)

- Local requirements and roadmap: `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `.planning/STATE.md`.
- Local code seams: `app.py`, `monitoring_board/services/fusionsolar.py`, `tests/test_fusionsolar_sync.py`, `tests/test_performance.py`, `tests/test_performance_backfill.py`, `tests/test_scheduler_safety.py`.
- Huawei SmartPVMS 24.5.0 Northbound API Reference search result: `https://support.huawei.com/enterprise/en/doc/EDOC1100379184` - confirms `getKpiStationDay` / `getKpiStationMonth` paths and addition of `PVYield` / `inverterYield` fields to plant KPI packets.
- Huawei SmartPVMS 24.4.0 Power Parameters: `https://info.support.huawei.com/enterprise/en/doc/EDOC1100358764/c142fea1/power-parameters` - confirms PV yield and inverter yield units as kWh.

### Secondary (MEDIUM confidence)

- Huawei SmartPVMS realtime API page: `https://support.huawei.com/enterprise/zh/doc/EDOC1100427894/a5add2fa` - search/open snippets confirm `/thirdData/getStationRealKpi`, `XSRF-TOKEN`, and `failCode=0` success semantics; page content was not fully extractable.
- Shelly Sigenergy integration notes: `https://kb.shelly.cloud/knowledge-base/kbuca-sigenergy-sigen` - detailed third-party notes on Sigenergy auth, JSON-string `data`, `sigen-region`, energy-flow fields, and rate limits. Useful but not official Sigenergy documentation.

### Tertiary (LOW confidence)

- Sigenergy public product/support pages found in search confirm a cloud/developer ecosystem exists, but public official API details were not accessible enough to verify exact error codes.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - repository already uses pytest, sqlite3, requests, and monkeypatch patterns.
- Architecture: HIGH - exact local code seams and tests were inspected.
- FusionSolar field/unit assumptions: MEDIUM - supported by Huawei docs/snippets and local implementation, but actual account packets still need fixture confirmation.
- Sigenergy field/rate-limit assumptions: LOW-MEDIUM - local code is clear, but public official API docs were not fully available; third-party notes should be validated with real provider samples when possible.

**Research date:** 2026-05-14  
**Valid until:** 2026-06-13 for local architecture; provider API assumptions should be refreshed when real FusionSolar/Sigenergy samples become available.
