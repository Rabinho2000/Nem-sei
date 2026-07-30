# Phase 3 Provider Assumptions

This note records what Phase 3 fixture-backed tests now pin locally and what still needs live-provider samples before the application should expand behavior or treat provider contracts as authoritative.

## Pinned By Fixtures

FusionSolar fixtures under `tests/fixtures/fusionsolar`:

- `stations_page_1.json`: station list shape with `data.list`, `plantCode`, and `stationCode` variants consumed by `fetch_fusionsolar_stations()`.
- `realtime_kpi.json`: realtime KPI rows with `real_health_state` values mapped to local statuses by `normalize_fusionsolar_plant_row()` and `map_fusionsolar_status()`.
- `alarms_active.json`: active alarm rows whose severities override healthy realtime state into local alert/error statuses.
- `kpi_day_rows.json`: daily KPI rows with `collectTime`, `PVYield`, `inverterYield`, and legacy `inverter_power` production values.
- `kpi_month_rows.json`: monthly KPI rows with the same production key families used by performance/backfill code.

FusionSolar tests in `tests/test_fusionsolar_provider_contracts.py` pin fetch helper request shapes, station/realtime/alarm normalization, current `collectTime` parsing behavior, production key priority, raw production values, and string-based `failCode` recognition for rate-limit/session-expiry helpers.

Sigenergy fixtures under `tests/fixtures/sigenergy`:

- `auth_success_json_string.json`: auth payload where `data` is a JSON-encoded string containing `accessToken` and expiry fields.
- `auth_success_object.json`: auth payload where `data` is already an object.
- `systems_list.json`: system-list response with fake stable systems.
- `realtime_data.json`: realtime system status and power fields.
- `energy_flow.json`: PV, grid, battery, SOC, and load flow fields.
- `error_code_payload.json`: non-zero provider-code response used for generic provider failure handling.

Sigenergy tests in `tests/test_sigenergy_provider_contracts.py` pin JSON-string/object payload parsing, token extraction, system-list variants, realtime and energy-flow normalization, generic non-zero code errors, and persistence of provider failures through `run_integration_sync()`.

## Remaining Assumptions

- FusionSolar collectTime timezone semantics are not authoritative yet. The app currently accepts milliseconds, seconds, ISO dates, and `dd/mm/yyyy` fallback strings, and timestamp conversion still follows the current host-local `datetime.fromtimestamp()` behavior. Real account samples are needed before changing this.
- FusionSolar production values are treated as kWh when selected from `PVYield`, then `inverterYield`, then legacy `inverter_power`. The tests pin current priority and raw value capture, but real account samples should confirm which key best represents customer-facing production for each target plant/API version.
- FusionSolar rate-limit and session-expiry handling still depends on string matching around `failCode` values, including `407`, `305`, and `USER_MUST_RELOGIN`. The tests pin current recognized payloads, not every provider message variant.
- Sigenergy provider-specific throttle and token-expiry API codes are not fully known. HTTP 401 refresh and HTTP 429 cooldown are handled explicitly; other non-zero codes remain operation-scoped provider errors.
- Sigenergy realtime and energy-flow freshness guarantees are still assumptions. Calls use the persistent account/API-area queue and its configurable cadence.
- A real account confirmed discovery `code=1201` while direct `energyFlow` and daily history remained available. Discovery is therefore optional and cannot gate direct access, sync or history.
- The former configured-system fallback was removed. `SIGENERGY_SYSTEM_IDS` is deprecated and never authorizes a system or supplies sync candidates.

## Do Not Rely On Yet

- Exact Sigenergy throttle, quota, or token-expiry code values beyond the generic non-zero error path.
- Authoritative FusionSolar timestamp timezone semantics for `collectTime`.
- Provider-wide guarantees that `PVYield`, `inverterYield`, and `inverter_power` always use the same unit and business meaning across all target accounts.
- Exhaustive FusionSolar `failCode` message text matching across languages or API versions.
- Sigenergy realtime and energy-flow freshness guarantees for large accounts or high-frequency scheduled syncs.
- Discovery results as proof that a known System ID is inaccessible; direct `energyFlow` is the independent access check.

## Regression Commands

Targeted FusionSolar provider regression:

```bash
python -m pytest -q tests/test_fusionsolar_provider_contracts.py tests/test_fusionsolar_service.py tests/test_fusionsolar_sync.py tests/test_performance.py tests/test_performance_backfill.py
```

Targeted Sigenergy provider regression:

```bash
python -m pytest -q tests/test_sigenergy_provider_contracts.py tests/test_scheduler_safety.py
```

Phase-level provider sanity command:

```bash
python -m pytest -q tests/test_fusionsolar_provider_contracts.py tests/test_sigenergy_provider_contracts.py tests/test_scheduler_safety.py tests/test_performance_backfill.py
```
