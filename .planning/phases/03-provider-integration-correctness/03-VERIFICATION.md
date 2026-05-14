---
phase: 03-provider-integration-correctness
status: passed
verified_at: 2026-05-14T17:50:29+01:00
verifier: codex
---

# Phase 03 Verification: Provider Integration Correctness

## Goal

Reduce risk around FusionSolar and Sigenergy parsing, timestamps, rate limits, and production units.

## Result

Phase 03 goal achievement is verified against the planning artifacts, fixture files, provider contract tests, and the relevant implementation seams in `app.py` and `monitoring_board/services/fusionsolar.py`. The phase reduced provider risk by pinning local provider-shaped response contracts, adding focused parser/normalizer regressions, and documenting the external-provider assumptions that still require real account samples before future behavior expansion.

## Must-Have Verification

### Plan 03-01: FusionSolar Fixture-Backed Provider Contract Coverage

- Passed: `tests/fixtures/fusionsolar/` contains station list, realtime KPI, active alarm, daily KPI, and monthly KPI JSON fixtures.
- Passed: fixtures are fake, stable, valid JSON, and include the planned response-shape keys such as `data.list`, `plantCode`, `stationCode`, `real_health_state`, `lev`, `collectTime`, `PVYield`, `inverterYield`, and `inverter_power`.
- Passed: `tests/test_fusionsolar_provider_contracts.py` uses fake session/response objects and does not perform live FusionSolar HTTP calls.
- Passed: `fetch_fusionsolar_stations()`, `fetch_fusionsolar_realtime_map()`, `fetch_fusionsolar_alarm_map()`, and `normalize_fusionsolar_kpi_rows()` are covered against fixture response shapes.
- Passed: `map_fusionsolar_status()` and `normalize_fusionsolar_plant_row()` are covered for healthy, faulty, disconnected, minor alarm, and critical alarm outcomes.
- Passed: `parse_fusionsolar_collect_date()` is covered for millisecond timestamps, ISO date strings, `dd/mm/yyyy` fallback values, and explicit fallback date behavior.
- Passed: `select_production_value()` is covered for the current production priority order: `PVYield`, then `inverterYield`, then `inverter_power`, including the selected raw value.
- Passed: `post_fusionsolar_json()` error payloads feed the existing `is_fusionsolar_rate_limit_error()` and `is_fusionsolar_session_expired_error()` recognizers for `failCode` 407 and 305 variants.

### Plan 03-02: Sigenergy Fixture-Backed Provider Contract Coverage

- Passed: `tests/fixtures/sigenergy/` contains auth JSON-string, auth object, systems list, realtime data, energy flow, and provider error JSON fixtures.
- Passed: fixtures are fake, stable, valid JSON, and include planned keys such as `accessToken`, `systemId`, `systemStatus`, `pvPower`, `batterySoc`, and non-zero provider `code`.
- Passed: `tests/test_sigenergy_provider_contracts.py` uses monkeypatched request boundaries and does not perform live Sigenergy HTTP calls.
- Passed: `parse_provider_payload_data()` is covered for JSON-string `data`, object `data`, and invalid string preservation.
- Passed: `get_sigenergy_token()` is covered for JSON-string and object token payloads, with `SIGENERGY_TOKEN_CACHE` cleared around tests.
- Passed: `normalize_sigenergy_system_rows()` is covered for `data.list`, list variants, and a single-system object.
- Passed: `normalize_sigenergy_system_row()` is covered for `external_id`, `external_name`, local status, raw status, notes, and payload composition from realtime and energy-flow fixtures.
- Passed: `fetch_sigenergy_json()` is covered for non-zero provider-code errors.
- Passed: `run_integration_sync()` persists Sigenergy provider failures through `integration_sync_runs.status = 'error'` and `integration_configs.last_error`.

### Plan 03-03: Provider Assumptions Note And Regression Command

- Passed: `.planning/phases/03-provider-integration-correctness/03-PROVIDER-ASSUMPTIONS.md` exists.
- Passed: the note separates fixture-pinned behavior from unresolved live-provider contracts for both FusionSolar and Sigenergy.
- Passed: timestamp, production-unit/key-priority, rate-limit, session/token expiry, response-shape, freshness, and configured-system fallback assumptions are explicit.
- Passed: the note lists targeted FusionSolar, targeted Sigenergy, and phase-level provider sanity pytest commands.
- Passed: the current phase-level provider sanity command passes.

## Tests Reviewed And Run

Reviewed:

- `.planning/ROADMAP.md`
- `.planning/STATE.md`
- `.planning/REQUIREMENTS.md`
- `.planning/phases/03-provider-integration-correctness/03-01-PLAN.md`
- `.planning/phases/03-provider-integration-correctness/03-01-SUMMARY.md`
- `.planning/phases/03-provider-integration-correctness/03-02-PLAN.md`
- `.planning/phases/03-provider-integration-correctness/03-02-SUMMARY.md`
- `.planning/phases/03-provider-integration-correctness/03-03-PLAN.md`
- `.planning/phases/03-provider-integration-correctness/03-03-SUMMARY.md`
- `.planning/phases/03-provider-integration-correctness/03-PROVIDER-ASSUMPTIONS.md`
- `tests/fixtures/fusionsolar/*`
- `tests/fixtures/sigenergy/*`
- `tests/test_fusionsolar_provider_contracts.py`
- `tests/test_sigenergy_provider_contracts.py`
- `app.py`
- `monitoring_board/services/fusionsolar.py`

Run:

```text
python -m json.tool tests/fixtures/fusionsolar/stations_page_1.json > $null; python -m json.tool tests/fixtures/fusionsolar/realtime_kpi.json > $null; python -m json.tool tests/fixtures/fusionsolar/alarms_active.json > $null; python -m json.tool tests/fixtures/fusionsolar/kpi_day_rows.json > $null; python -m json.tool tests/fixtures/fusionsolar/kpi_month_rows.json > $null
```

Result:

```text
passed
```

Run:

```text
python -m json.tool tests/fixtures/sigenergy/auth_success_json_string.json > $null; python -m json.tool tests/fixtures/sigenergy/auth_success_object.json > $null; python -m json.tool tests/fixtures/sigenergy/systems_list.json > $null; python -m json.tool tests/fixtures/sigenergy/realtime_data.json > $null; python -m json.tool tests/fixtures/sigenergy/energy_flow.json > $null; python -m json.tool tests/fixtures/sigenergy/error_code_payload.json > $null
```

Result:

```text
passed
```

Run:

```text
python -m pytest -q tests/test_fusionsolar_provider_contracts.py tests/test_fusionsolar_service.py tests/test_fusionsolar_sync.py tests/test_performance.py tests/test_performance_backfill.py
```

Result:

```text
48 passed in 4.93s
```

Run:

```text
python -m pytest -q tests/test_sigenergy_provider_contracts.py tests/test_scheduler_safety.py
```

Result:

```text
11 passed in 1.12s
```

Run:

```text
python -m pytest -q tests/test_fusionsolar_provider_contracts.py tests/test_sigenergy_provider_contracts.py tests/test_scheduler_safety.py tests/test_performance_backfill.py
```

Result:

```text
37 passed in 4.81s
```

## Gaps And Human Checks

- No code gaps found for the Phase 03 planned must-haves.
- Human/provider check: FusionSolar `collectTime` timezone semantics still need real account samples before changing the current host-local `datetime.fromtimestamp()` behavior.
- Human/provider check: FusionSolar production key meaning and units still need real account confirmation before treating `PVYield`, `inverterYield`, or `inverter_power` as universally authoritative across deployments.
- Human/provider check: FusionSolar failCode coverage is not exhaustive across languages/API versions; current regressions only pin the recognized 407/305/session-expiry shapes.
- Human/provider check: Sigenergy exact throttle, quota, and token-expiry codes still need real provider samples; current behavior pins generic non-zero provider-code handling and error persistence.
- Human/provider check: Sigenergy realtime/energy-flow freshness and configured-system fallback behavior should be revalidated with production accounts before expanding sync behavior.
- Working tree note: unrelated pre-existing modified and untracked files were present during verification. This verification only adds `03-VERIFICATION.md` and does not change application behavior.
