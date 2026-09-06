# Availability time: functional migration plan (FusionSolar scope)

Status: **plan confirmed by the user (§ open questions, all three answered
with the recommended option) and the core engine implemented, 2026-09-04.**
See "Implementation status" at the end for exactly what landed, what was
run, and what is still gated. Supersedes nothing in `DEVICE_TELEMETRY.md`
(still authoritative on the device-status contract and canary history) —
this document only adds the aggregation layer on top of what that milestone
already built.

## 0. What already exists (read before building anything new)

This is not a greenfield feature. Three of the nine requested items are
already substantially done:

- **Raw device samples already persist** (`diagnostics/models.py`,
  `DeviceStatusFact`, migrations 0015/0016): `device_id`, `asset_id`,
  `observed_at`, `availability_status`, `active_power_kw`, `day_energy_kwh`,
  `freshness`/`quality`/`completeness`, `source_kind` (`v1_import` backfill +
  `live_read`). This *is* item 2's "device sample facts" — no new fact table
  is needed for FusionSolar.
- **Live collection already runs in production** (`FusionSolarDeviceStatusService`,
  `integrations/fusionsolar/device_status.py`, M7 Fatia 2/3): polls
  `getDevList` + `getDevRealKpi` every 30 minutes for connection 3 / asset 153
  (2 inverters), verified units, honest `freshness="unknown"` when
  `collectTime` is absent (it always is, per the canary). Went live in
  production 2026-08-25 (`docs/v2/DEVICE_TELEMETRY.md` §10.1), capped at 1443
  cycles (~30 days, stops ≈2026-09-24). **As of today (2026-09-04) that is
  ~10 days of continuous real data for asset 153** — enough to validate a
  full operating day end-to-end, which the 70-minute Fatia-3 canary window
  explicitly could not do (§8.4 of that doc).
- **The weighting math is already ported and golden-tested**:
  `reporting/rules/availability.py::weighted_sampled_availability` /
  `float_or_none`, pinned against V1's `_weighted_sampled_availability` /
  `_float_or_none` in `tests_v2/test_availability_golden.py`. This is the one
  piece of V1's logic that needed no persisted facts to port. Its own
  docstring says persistence "waits for device-level facts" — that blocker is
  gone; this plan is what removes the sentence.
- **The reporting hook points already exist**, deliberately built as an
  honest gap rather than a guess: `assembler.py`'s
  `AVAILABILITY_FIELDS_WITHOUT_SOURCE = ("availability_pct",)`,
  `include_availability_kpi: False`, and `templates/portfolios/availability.html`
  (ships today as a placeholder that literally says the math is ready and
  waiting on device facts).
- **"Expected devices for a date" needs no new table.** V1 built
  `provider_device_configuration_history` because `provider_devices` had no
  temporal validity. V2's `asset_provider_mappings` already carries
  `valid_from`/`valid_to`/`mapping_status` per device
  (`resource_kind='device'`), and `Device.device_kind`/`lifecycle_status`
  already replace V1's `dev_type_id IN (1, 38)` string-matching hack
  (`is_removed_inverter_name`) with real canonical fields. This removes an
  entire table and its seeding logic from the port.

Net effect: **items 2 and 3 are ~90% done already.** What is genuinely new
work is the window/gap/coverage materializer (item 4), the daily/rollup
storage for its *output* (item 5), and wiring it into reporting (item 6).

### ⚠️ Live collision warning

This working tree currently has **uncommitted changes from a concurrent
session** touching exactly this area: `config.py`, `jobs/repository.py`,
`integrations/fusionsolar/device_status.py`, `docker-compose.v2*.yml`, and
`docs/v2/{DEVICE_TELEMETRY,KNOWN_GAPS,PIPELINE_HEALTH,ARCHITECTURE,HUAWEI_SCADA}.md`
(24 files, ~1050 lines). It looks like O&M-scoped polling
(`production_om_scope_enabled`) and deployment-contract work, not this
feature — but it is not committed, so it is not yet a stable base to branch
new migrations from. **Before writing any migration or touching
`device_status.py`/`config.py`/`jobs/*`, that work needs to either land
(commit) or be coordinated with**, or this feature's new code should be kept
in new, untouched files only until it does. This plan is written so that
almost everything new lives in new files for exactly that reason.

## 1. V1 logic, mapped exactly

V1 has **two independent, divergent availability engines**, not one. This
matters because the task description's rule list (15-minute granularity +
30-minute edge tolerance + 90-minute gap + missing/partial/complete) is a
blend of both — they need to be told apart before porting either.

| | `reporting/availability.py` ("slot" engine) | `services/sampled_availability.py` ("sampled" engine) |
|---|---|---|
| Raw data | `inverter_power_samples`, filled once/day from a **separate FusionSolar device-history batch call** | `device_realtime_snapshots`, filled by the **ongoing realtime poll** (same cadence as current-monitoring) |
| Bucketing | Explicit 15-minute slots (`inverter_availability_slot`) | None — works on raw sample instants |
| Edge rule | Trims 30 min off *each day's own* first/last **observed** slot (`apply_inverter_edge_tolerance`) | Same 30-minute tolerance, but applied to the *production window* (first→last positive-power sample), not calendar slots |
| Gap rule | none | `MAX_SAMPLE_GAP_MINUTES = 90`, checked between consecutive samples per device |
| Minimum samples | none | `max(4, ceil(duration_minutes / 90) + 1)` |
| Coverage states | availability_pct or `None` | explicit `sampled_complete` / `sampled_partial` / `missing` / `indeterminate`, with warning codes (`late_first_sample`, `early_last_sample`, `sample_gap_over_90_minutes`, `insufficient_sample_count`, `missing_expected_inverter`, …) |
| Output tables | `inverter_availability_daily`, `plant_availability_daily` | `inverter_availability_sampled_daily`, `plant_availability_sampled_daily` |
| **What actually feeds V1's reports/Excel/monthly close today** | **Yes** — `repositories.get_monthly_availability()` reads `plant_availability_daily` (confirmed: called from `monthly_close.py`, `portfolio_reports.py`, `app_factory.py:10713`) | **No** — feeds only an internal diagnostics panel (`_production_api_queue.html`) and `sampled_month_quality`; never reached the customer-facing report |
| Historical density | dense (once/day batch pull, real slots) | **sparse**: V1's own realtime poll rarely ran long/densely enough — only 3 of 6 720 device-days ever reached `sampled_complete` (`DIAGNOSTICS.md`) |

**Decision: port the *sampled* engine, not the slot engine.** Reasons:

1. It is the one the task description actually specifies — gap/tolerance/
   missing-partial-complete are its vocabulary, not the slot engine's.
2. It is the one V2 already has raw data for: `device_status_facts` is a
   realtime-poll table, structurally identical to `device_realtime_snapshots`,
   not to a daily batch-history pull. Porting the slot engine would mean
   building an entirely new FusionSolar device-history collector V2 has no
   evidence for and no current need of — a second API surface, a second raw
   table, for a *weaker* algorithm. That is the opposite of "avoid extra API
   calls."
3. It already has richer, more honest semantics (explicit missing/partial/
   complete, explicit warning codes) that match V2's house style elsewhere
   (`QUALITY_STATES = (complete, partial, missing, invalid, unknown)` is
   already V2's vocabulary in `monitoring/models.py` and
   `diagnostics/models.py` — the sampled engine's states drop in natively).
4. The slot engine being V1's production number is V1 debt, not a
   requirement: it was denser only because it made one extra API call per
   day that V2 does not need to make, now that `device_status_facts` is
   populated by a poll that already runs for other reasons (current
   monitoring / diagnostics). Reproducing it would cost real, avoidable
   FusionSolar calls against a shared, contended account — directly against
   this task's own efficiency instruction.

"15-minute slots" in the task description is read as describing the sampled
engine's effective cadence class, not a literal request to build the slot
engine. This is flagged explicitly rather than silently decided — worth a
one-line confirmation before Fase 2 starts (see §7, open question 1).

## 2. Persisted facts (item 2)

**Device-level raw samples: already exist, nothing to add.**
`device_status_facts` (FusionSolar `live_read` rows) is the direct analogue
of V1's `device_realtime_snapshots`, with the added benefit of `quality`/
`completeness`/`freshness` V1 never recorded and honest `unknown` instead of
V1's silently-assumed-fresh default.

**New: two small materialized daily-aggregate tables**, mirroring V1's
`inverter_availability_sampled_daily` / `plant_availability_sampled_daily`
one-for-one, adapted to V2's schema (`device_id`/`asset_id` FKs, V2's
`QUALITY_STATES` vocabulary instead of V1's ad hoc strings, JSON warning list
instead of a comma-joined string):

```
device_availability_daily
  id, device_id (FK devices), asset_id (FK assets), availability_date,
  availability_pct (nullable), valid_sample_count, minimum_required_samples,
  coverage_status ('complete'|'partial'|'missing'|'indeterminate'),
  warning_codes (JSON array), operational_window_start/end (tz-aware),
  source ('fusionsolar_sampled'), calculated_at, created_at, updated_at
  UNIQUE(device_id, availability_date)

asset_availability_daily
  id, asset_id (FK assets), availability_date, availability_pct (nullable),
  valid_sample_count, expected_device_count, observed_device_count,
  coverage_status, warning_codes (JSON array),
  operational_window_start/end, minimum_required_samples,
  calculation_details_json, source, calculated_at, created_at, updated_at
  UNIQUE(asset_id, availability_date)
```

These are **idempotent materialized aggregates**, recomputed (delete+insert
per key, exactly like V1's `_store_sampled_result`), not append-only facts —
same distinction the codebase already draws between `production_facts`
(append-only, revisioned) and a `ReportingDataset` (recomputed on demand).
There is nothing here to supersede; re-running the day's materializer is the
correction mechanism, same as V1.

**No new table for installation/portfolio level.** V1 never persisted those
either — it aggregates asset rows on read. V2 does the same: installation
availability = `weighted_sampled_availability` over its member assets'
`asset_availability_daily` rows (weighted by asset `installed_power_kwp` if
that exists, else count-mean, matching `calculate_weighted_portfolio_availability`'s
V1 logic); portfolio availability = the same function one level up, using
`portfolios`' existing flat, temporal membership (`nemsei-v2-portfolios`
memory). Zero new tables, zero new migration surface for that part.

## 3. FusionSolar collection (item 3)

**Already built, already live** (`FusionSolarDeviceStatusService`). No new
collection code is proposed. The materializer in §4 is a **pure read of
already-persisted `device_status_facts`** — it makes zero provider API
calls. This is the direct answer to "evitando fazer mais calls da API": the
aggregation step is decoupled from ingestion entirely, exactly like V1's own
`materialize_sampled_availability_day` was a pure-SQLite function called
*after* a sync had already written its rows, never a function that goes back
to the provider.

Two ways to trigger materialization, both zero-extra-call:

1. **Inline, right after each successful `device_status.poll` job** (mirrors
   V1's own `run_fusionsolar_check` → `materialize_sampled_availability_day`
   call in `app_factory.py:21490`) — keeps today's aggregate fresh within one
   poll cycle, no separate schedule needed.
2. **A `nemsei availability materialize` CLI command** for backfill/repair
   over a date range, reading only what is already in `device_status_facts`
   (mirrors V1's `materialize_existing_sampled_availability` /
   `cleanup_realtime_snapshot_payloads` pattern) — this is what item 7's
   validation run uses.

Both call the same pure function; there is no third code path.

## 4. Porting the rules (item 4)

New module `src/nemsei/reporting/rules/availability_window.py` (kept out of
`diagnostics/` and `integrations/` on purpose — pure calculation, no I/O, so
it can be golden-tested standalone exactly like `rules/availability.py`
already is).

Ported 1:1 from `materialize_sampled_availability_day` / `_store_sampled_result`:

- `OPERATING_EDGE_MINUTES = 30`, `MAX_SAMPLE_GAP_MINUTES = 90` — same
  constants, same names, so a diff against V1 stays legible.
- Operational window = `[min, max]` of timestamps with `active_power_kw > 0`,
  computed **per Lisbon calendar day** (`zoneinfo("Europe/Lisbon")`, ported
  verbatim — this is where DST-transition test cases matter, see §6).
- `minimum_required_samples = max(4, ceil(duration_minutes / 90) + 1)`.
- Per-device warning codes: `late_first_sample`, `early_last_sample`,
  `sample_gap_over_90_minutes`, `insufficient_sample_count`,
  `missing_expected_inverter` — same triggers, same names.
- Coverage vocabulary translated to V2's existing `QUALITY_STATES`:
  `sampled_complete → complete`, `sampled_partial → partial`, `missing →
  missing`, `indeterminate → indeterminate` (kept as a 4th state, since it
  means something V1's `missing` doesn't: expected devices exist but no
  positive-power sample was ever observed — a different fact than "no
  configuration at all").
- Asset-level `coverage_status` = `complete` **iff every expected device is
  complete**, same all-or-nothing rule as V1 (`plant_availability_sampled_daily`).
- `availability_pct` only ever set when `coverage_status == complete` — never
  a number attached to a partial/missing day, ported as a hard invariant
  (mirrors V1's `if coverage_status != FINAL_SAMPLED_STATUS: availability_pct = None`).

Not ported: `record_device_configuration` /
`provider_device_configuration_history` (§0 — superseded by
`asset_provider_mappings`' native temporal fields) and the 15-minute slot
engine (§1, not the target).

## 5. Aggregation by inverter / installation / portfolio (item 5)

- **Inverter (device):** `device_availability_daily`, one row per device per
  day, as built by §4.
- **Asset (plant):** `asset_availability_daily`, weighted by
  `Device.rated_power_kw` via the already-golden `weighted_sampled_availability`
  — no new weighting logic, reuse verbatim.
- **Installation:** computed on read from its member assets'
  `asset_availability_daily` for the requested period — an installation can
  carry >1 asset in V2 (unlike V1, where installation ≡ asset), so this is a
  genuinely new rollup, not present in V1 at all. Weight by each asset's
  installed capacity; an asset with `coverage_status != complete` makes it
  contribute `None`, which — per `weighted_sampled_availability`'s existing
  rule — makes the *whole installation* `None` for that day rather than
  silently averaging over fewer assets. This is a deliberate, conservative
  choice worth flagging to the user (see §7, open question 2): V1 had no
  multi-asset installations, so there is no V1 behavior to be "parity" with
  here — this is new policy, not a port.
- **Portfolio:** same rollup one level higher over portfolio membership for
  the period (temporal, per `nemsei-v2-portfolios` memory), reusing
  `calculate_weighted_portfolio_availability`'s already-ported logic.
- `valid_slots`/coverage exposed at every level exactly as V1 named them
  (`valid_snapshot_count`, `minimum_required_snapshots`, `coverage_status`),
  so the Excel/report columns keep the same header vocabulary users already
  know from V1.

## 6. SCADA feasibility (explicitly requested)

Checked `huawei_scada_power_samples` (migration 0026,
`integrations/huawei_scada/ingestion.py`): it is **plant-level only** —
`pv_input_power_kw`/`load_power_kw`/`grid_power_kw`/`battery_power_kw`/
`total_active_power_kw` keyed on `asset_id`/`provider_mapping_id`, with no
`device_id` and no per-inverter breakdown at all (one SDongle aggregate
block per plant). It also has no status/condition register — "operational"
is already derived the same way `energyFlow` forced Sigenergy to be derived
(`nemsei-v2-sigenergy` memory): `generating = pv_input_power_kw > 0 or
total_active_power_kw > 0`.

**Conclusion: SCADA can contribute a plant-level cross-check, not an
inverter-level availability figure.** Concretely, in scope for this plan:

- Where a plant has both a live FusionSolar device mapping *and* a Huawei
  dongle, `asset_availability_daily`'s calculated operational window
  (first/last positive `active_power_kw` across all inverters) can be
  cross-validated against the SCADA aggregate's own positive-power window
  for the same day — a same-source-independent sanity check with zero extra
  cost (the samples already exist for other reasons), surfaced as an extra
  `metadata_json` field (`scada_window_agrees: bool`), never as a silent
  override of the FusionSolar-derived number.
- **Not proposed:** using SCADA power alone to *compute* `availability_pct`
  for a plant with no device-level FusionSolar mapping. Without per-inverter
  identity there is no way to tell "one of three inverters is down" from
  "the whole plant is down" — exactly the distinction `availability_pct`
  exists to make. A SCADA-only plant could still get an asset-level
  operational/non-operational daily flag (reusing the same window logic
  minus the per-device rollup), but that is a materially weaker metric and
  should not be labelled `availability_pct` if built — flagged as a
  possible *separate*, later item, not silently folded into this one.

## 7. Read-only validation before any production import (item 7)

**No blind import of V1's historical `inverter_availability_sampled_daily`/
`plant_availability_sampled_daily` values.** Two reasons this needs to be
read-only comparison, not a one-shot import:

1. V1's own realtime poll was sparse enough that only **3 of 6 720
   device-days** ever reached `sampled_complete`
   (`DIAGNOSTICS.md`) — a broad historical day-by-day diff against V1 is not
   meaningful; there is almost nothing complete to diff against.
2. V1 is stopped (`nemsei-v1-desligado` memory) and was stopped before this
   feature's live V2 data collection window began (2026-08-25) — the two
   systems never sampled the *same* days at the *same* density, so a
   fresh-data comparison against V1's live behavior is not possible either.
   V1's SQLite is a frozen historical artifact from here on, exactly the way
   the existing golden tests already treat it (`V1_ROOT` read-only mount).

**What "validate before activating" concretely means here, given that:**

- **Primary assurance: algorithmic parity, not historical-data parity.**
  Extend `tests_v2/test_availability_golden.py` with synthetic per-device
  sample sequences run through both `availability_window.py` and V1's
  `materialize_sampled_availability_day` (imported read-only from
  `V1_ROOT`, same `requires_v1`/`skipif` pattern already established, so CI
  without the V1 mount degrades gracefully instead of failing —
  `nemsei-v2-test-runner` memory). This is the load-bearing check.
- **Secondary, opportunistic: the 3 real complete V1 device-days**, used as
  a real-world spot check (same device, same day, same inputs pulled from
  V1's frozen `device_realtime_snapshots`, fed through V2's ported function,
  expect the same `availability_pct`/`coverage_status`).
- **Forward validation: `nemsei availability compare` CLI**, read-only,
  reporting only, no writes to any reporting table: for asset 153 (the one
  asset with ~10 days of continuous live `device_status_facts` right now),
  compute daily coverage_status/availability_pct and print it next to what
  the same window would have looked like if fed through V1's function
  directly (same input, both algorithms — since they're now the same
  algorithm by construction, this mostly re-confirms §4's port, but also
  finally answers `DEVICE_TELEMETRY.md` §8.4/§10.1's open question: **does
  ~10 days of real 30-minute-cadence data clear `late_first_sample`/
  `early_last_sample`/`sample_gap_over_90_minutes`/`insufficient_sample_count`
  for real full operating days?** That question predates this plan and this
  plan's Fase 4 is what finally answers it with real data instead of
  extrapolation.
- Only after that comparison is reviewed by a human does §8's reporting
  wiring (`include_availability_kpi = True`) get flipped — mirrors the
  project's own established discipline for consequential live changes
  (`DEVICE_TELEMETRY.md` §10's human-sign-off-before-flip pattern).

## 8. Reporting integration (item 6), gated on §7's sign-off

- `reporting/assembler.py`: drop `"availability_pct"` from
  `AVAILABILITY_FIELDS_WITHOUT_SOURCE`, flip `include_availability_kpi` to a
  real read (`asset_availability_daily`/rollup) instead of the hardcoded
  `False` — both are existing lines to edit, not new plumbing.
- `reporting/datasets.py`: add `availability_pct` (and `coverage_status`, for
  an honest "provisional" indicator) as a real `DATASET_METRICS` row.
- `reporting/excel.py` / `portfolio_excel.py` / `financial_workbook.py`:
  unhide the existing (currently always-`None`) availability column.
- `templates/portfolios/availability.html`: replace the placeholder
  paragraph and the coverage-only table with the real per-installation
  `availability_pct` + `state_pill(coverage_status)`, keeping a fallback row
  for installations still `partial`/`missing`/`indeterminate` rather than
  hiding them.
- Sigenergy (item 9): the materializer only ever runs for
  `ProviderCode.FUSIONSOLAR`; Sigenergy assets keep `availability_pct` in
  `unavailable_fields` unconditionally until a device-level contract exists
  for it (`nemsei-v2-sigenergy` memory — none does today). No code branch is
  proposed that would let Sigenergy fall through to a number.

## 9. Tests (item 8)

- `tests_v2/test_availability_window.py` (new): golden cases for
  `availability_window.py`, covering — missing expected-device configuration;
  no positive-power sample at all (`indeterminate` vs `missing`); a clean
  full day (`complete`); a device with a >90-minute gap mid-day; a device
  whose first sample lands >30 min after window start
  (`late_first_sample`); one ending >30 min before window end
  (`early_last_sample`); below-minimum sample count on a short window;
  one-of-N devices missing (asset stays `partial`, never silently drops to
  N-1); negative/zero/non-numeric `active_power_kw` (must not count as
  "positive" and must not crash the window calc — mirrors
  `_positive_float`); a day straddling the Europe/Lisbon DST transition
  (2026's spring-forward/fall-back dates), asserting slot/window boundaries
  use Lisbon wall-clock, not UTC.
- `tests_v2/test_availability_golden.py` (extend): the two `requires_v1`
  parity blocks from §7 (synthetic-input parity, 3-real-day spot check).
- `tests_v2/test_availability_rollup.py` (new): installation/portfolio
  weighting, including the "one asset partial makes the installation `None`"
  rule from §5, ported/adapted from V1's
  `test_inverter_and_weighted_plant_availability` /
  `test_report_excludes_removed_inverter_and_weights_power_from_model`
  (`Nem-sei/tests/test_fusionsolar_availability.py`) as source cases.
- `tests_v2/test_availability_reporting.py` (new): assembler/dataset wiring
  — `unavailable_fields` no longer lists `availability_pct` once a value
  exists; still lists it for Sigenergy; Excel column round-trip.

## 10. Migrations

One new migration (next free revision after whatever the concurrent
uncommitted work lands as — **must be re-checked at implementation time**,
not assumed to be `0039`): creates `device_availability_daily` and
`asset_availability_daily` only. No change to any existing table
(`device_status_facts`, `asset_provider_mappings`, `devices` all already
carry what this needs — see §0/§2).

## 11. Planned execution commands (for the implementation turn, not run yet)

```bash
alembic upgrade head
pytest tests_v2/test_availability_window.py tests_v2/test_availability_golden.py \
       tests_v2/test_availability_rollup.py tests_v2/test_availability_reporting.py -q
nemsei availability materialize --asset-id 153 --from 2026-08-25 --to 2026-09-03   # backfill from already-collected facts, zero API calls
nemsei availability compare --asset-id 153 --from 2026-08-25 --to 2026-09-03 --dry-run
pytest tests_v2 -q   # full suite, no regressions
```

## 12. Gaps that still block turning this on in production

1. **Single-asset live density.** Only asset 153 / connection 3 has live
   `device_status_facts` at the required cadence today; portfolio-wide
   rollout is gated on the same shared-FusionSolar-account contention
   `DEVICE_TELEMETRY.md` §4/§10 already identified — a separate, later
   decision, not something this plan's code changes.
2. **V1 comparison data is structurally sparse** (§7) — parity assurance
   rests on the algorithmic golden tests, not on a broad historical diff,
   because V1 itself rarely produced a complete day to diff against.
3. **Full-day edge coverage still needs to be checked against real data**
   (`DEVICE_TELEMETRY.md` §8.4 flagged this as open at the 70-minute-window
   stage) — Fase 4's compare run against the ~10 days now available is what
   closes it, but that run has not happened yet.
4. **Concurrent uncommitted work in the same area** (§0) — needs to land or
   be coordinated with before this feature's migration is written, to avoid
   a merge conflict on `config.py`/`jobs/repository.py`.
5. **Sigenergy stays blocked by design** (item 9, §8) — no device contract,
   not proposed to change here.
6. **Human sign-off needed before flipping `include_availability_kpi`**
   (§7/§8) — this plan proposes the mechanism, not the go-ahead to expose it
   in customer-facing reports/Excel.

## Addendum, 2026-09-04 (later session): monthly aggregation and full reporting integration

A later session in the same day re-verified the checkout from scratch
(nothing assumed from the summary above) and extended the work with the two
items that summary had explicitly left as gaps: monthly/portfolio
aggregation and the datasets/reports/Excel/page wiring (item 6). New,
independently-verified in this addendum:

- **`monthly_availability_for_asset`** (`diagnostics/availability_service.py`),
  ported from V1's `sampled_month_quality`, with one explicit, documented
  deviation: a month with **zero** materialized days now reports `missing`
  (V1's own function can only ever return `sampled_complete`/`sampled_partial`,
  even for zero rows — see the function's own docstring for the exact reason).
- **`reporting_dataset_rows.availability_pct`/`availability_state`**
  (migration `0040_reporting_availability`) — `build_dataset` now computes
  them per asset-month directly from `asset_availability_daily`, the same
  missing-implies-null shape every other metric on that row already has.
- **`assembler.py`**: `AVAILABILITY_FIELDS_WITHOUT_SOURCE` retired —
  `availability_pct`/`include_availability_kpi` are real now, gated
  per-period on `aggregate["availability_state"]` exactly like every
  `DATASET_METRICS` field already is (not unconditionally absent, not
  unconditionally present).
- **PDF**: `customer_pdf.py` already had the full V1-ported KPI-card gate
  (`include_availability_kpi`/`availability_pct`, `kpi_icon_kind`'s `"target"`
  entry) sitting unused, built ahead of its data source. No PDF renderer
  change was needed — wiring the assembler was enough to make it render a
  live "Disponibilidade (%)" card end-to-end (proven by test, not assumed).
- **Excel**: added to "Qualidade dos dados", not to the parity-locked
  "Resumo" KPI grid — that sheet is a verbatim copy of one real V1 export,
  and V1's own historical sparsity means no real captured V1 sample ever
  had availability populated to copy an exact row position from.
- **Portfolio rollup** (`portfolios/datasets.py`): availability handled as a
  dedicated non-summed metric (like `performance_pct` already was), weighted
  by `installed_dc_power_kw` via `rollup_availability` — one member short of
  `measured` makes the whole portfolio figure `None`, proven by test.
- **`portfolios/availability.html`**: placeholder replaced with real
  per-installation `availability_pct`/`availability_state` columns plus a
  portfolio total line.
- **13 new tests** across `test_availability_service.py` (+3 monthly),
  `test_reporting_datasets.py` (+2), `test_report_assembler.py` (+3),
  `test_portfolios.py` (+2), all passing against real PostgreSQL — see the
  session's own evidence block for exact commands and output.
- **End-to-end CLI proof, this session, against a disposable database
  seeded with real V1 asset 1888 device data** (not asset 153 — no
  production credentials were available to this session either): the
  official `v2_availability_materialize.py` and `v2_availability_compare.py`
  commands were run as actual subprocesses (not called as Python functions)
  against a fresh ephemeral Postgres database, produced real JSON output,
  exit code 0, and `engine_agrees_with_v1: true` for all 3 real days
  compared. The database was dropped immediately after.
- Full `tests_v2` regression suite re-run after all of the above — see the
  session's own evidence for the pass/fail count.

## Open questions — resolved 2026-09-04

All three answered with the recommended option:

1. Port the **sampled** engine, not V1's slot engine. **Confirmed.**
2. Installation rollup: **any incomplete member asset → `None`** for the
   whole installation, not an average over the complete subset. **Confirmed.**
3. Concurrent uncommitted session (§0): **proceed in parallel, new files
   only.** **Confirmed** — see "Implementation status" for exactly which
   existing files were (and were not) touched.

## Implementation status (2026-09-04)

Built in this session, on top of the confirmed plan above. Everything below
was actually run, not just written.

### What was built

- **`src/nemsei/reporting/rules/availability_window.py`** (new) — the ported
  sampled engine: `compute_asset_day_availability` (device+asset window/gap/
  coverage, §4), `rollup_availability` (installation/portfolio weighting,
  §5), `lisbon_day_bounds` (DST-safe day boundaries, with a documented trap:
  never subtract its two return values directly in Python — see the
  function's own docstring for why, and `_absolute_span` in the test file
  for the fix).
- **`src/nemsei/diagnostics/models.py`** (extended) — `DeviceAvailabilityDaily`,
  `AssetAvailabilityDaily` (§2). Not a new file: kept next to
  `DeviceStatusFact`, the raw evidence these are computed from.
- **`migrations/versions/0039_availability_daily.py`** (new) — creates
  exactly those two tables. Nothing else in the schema changed. `alembic
  check` confirms zero drift between the models and this migration.
- **`src/nemsei/diagnostics/availability_service.py`** (new) — the
  materializer: `expected_devices_for_date` (reads `asset_provider_mappings`/
  `devices`, FusionSolar-scoped by construction — item 9), `materialize_
  asset_availability_day` / `materialize_existing_availability` (§3, zero
  provider calls, idempotent), `installation_availability_for_date` /
  `portfolio_availability_for_date` (§5 rollups, reading only already-
  materialized `asset_availability_daily`, never recomputing).
- **`scripts/v2_availability_materialize.py`** (new) — the backfill/repair
  CLI (§3/§11), zero provider calls.
- **`scripts/v2_availability_compare.py`** (new) — the read-only V1
  comparison tool (§7): feeds V1's *real* historical `device_realtime_
  snapshots` through V2's ported engine and diffs the result against V1's
  own already-stored `plant_availability_sampled_daily` for the same day.
  Writes nothing to either database.
- **Existing files touched, both outside the concurrent session's changed
  set**: `src/nemsei/reporting/rules/availability.py` untouched (only
  imported from); no changes to `config.py`, `jobs/*`,
  `integrations/fusionsolar/device_status.py`, or any docker-compose/docs
  file the concurrent session had open.
- **Not built yet, on purpose (§6/§8 of "gaps" below)**: the
  `assembler.py`/`datasets.py`/`excel.py`/`portfolios/availability.html`
  wiring. The mechanism these would read from now exists and is tested; the
  actual flip is still gated on the human sign-off this plan's §7 calls for.

### Tests written and run

All commands below were actually executed against a real PostgreSQL
(`nemsei_v2_test` / ephemeral per-test databases at
`127.0.0.1:55432`) and the real, frozen V1 SQLite checkout, not just
written:

```bash
PYTHONPATH=src pytest -q tests_v2/test_availability_window.py     # 23 passed
PYTHONPATH=src pytest -q tests_v2/test_availability_golden.py     # 38 passed
PYTHONPATH=src pytest -q tests_v2/test_availability_service.py    # 10 passed
```

- **`test_availability_window.py`** (new, item 8's quality coverage): missing
  configuration, zero samples, never-positive-power, a clean complete day,
  a >90-minute gap, late/early edge samples, below-minimum sample count, a
  missing-but-expected device, invalid/negative/NaN power (never crashes,
  never counts as positive), unavailable-status lowering `availability_pct`
  within an otherwise-complete day, and three DST-boundary tests (Portugal's
  2026 spring-forward and fall-back dates, plus an ordinary day) — the last
  three also caught and documented a real CPython pitfall (aware-datetime
  subtraction silently ignoring a DST offset change when both operands share
  the identical `tzinfo` object), fixed in both the test and the function's
  own docstring before it could reach production code.
- **`test_availability_golden.py`** (extended, item 8's parity requirement):
  the pre-existing weighting-function parity block, plus two new blocks —
  (a) 7 synthetic scenarios run through **V1's real
  `materialize_sampled_availability_day`** (via an in-memory SQLite built to
  V1's actual schema) and compared field-by-field against V2's port; (b) a
  **real-data** parity block: V1's actual historical `device_realtime_
  snapshots` for **10 real FusionSolar assets** (2026-05-18 through
  2026-07-13, the densest window found by scanning V1's live database),
  fed through V2's port and compared against what V1's own materializer
  already computed and stored for the same days — **390 real device-day
  comparisons, 390/390 agreement, zero discrepancies.** (0 of those 390
  reached `coverage_status='complete'` in this particular window — expected,
  consistent with V1's own documented 3-of-6720 historical sparsity;
  `partial`/`missing`/`indeterminate` all matched exactly.)
- **`test_availability_service.py`** (new, DB round-trip): expected-device
  resolution (FusionSolar-only, temporal), Sigenergy structurally returning
  zero expected devices (item 9), a full materialize→persist→read cycle
  weighted by rated power, idempotent re-materialization, `v1_import` facts
  correctly excluded from the live window (only `source_kind='live_read'`
  counts), range backfill via `materialize_existing_availability`, and the
  installation-level "one incomplete member → `None` for the whole
  installation" rule (§5) proven against real materialized rows, not just
  the pure `rollup_availability` function.
- Also re-run for regression safety after every change:
  `tests_v2/test_diagnostics.py`, `test_diagnostics_import.py`,
  `test_diagnostics_golden.py`, `test_devices.py`,
  `test_architecture_boundaries.py` — all passing, no cross-boundary
  violation from `diagnostics/models.py` importing
  `reporting.rules.availability_window`.
- **Full `tests_v2` suite, run to completion**: `1636 passed, 4 failed` in
  14m55s. The 4 failures are all in `test_pdf_golden.py`
  (`test_v2_draws_the_same_pages_as_v1`, a PDF page-text golden comparison
  for the customer report's production chart) — **pre-existing, unrelated
  to this work**: neither `test_pdf_golden.py` nor
  `reporting/customer_pdf.py` was touched by this session or appears in the
  concurrent session's uncommitted diff; the branch's tip commit
  (`a923be7`, before this session started) changed the PDF's production
  chart ("Corrigir gráfico de produção vazio no relatório PDF") without
  updating this golden fixture. Flagged separately, not fixed here (out of
  this task's scope).

```
1636 passed, 4 failed in 895.63s (0:14:55)
FAILED tests_v2/test_pdf_golden.py::test_v2_draws_the_same_pages_as_v1[epc complete]
FAILED tests_v2/test_pdf_golden.py::test_v2_draws_the_same_pages_as_v1[esco model]
FAILED tests_v2/test_pdf_golden.py::test_v2_draws_the_same_pages_as_v1[everything missing]
FAILED tests_v2/test_pdf_golden.py::test_v2_draws_the_same_pages_as_v1[zero production]
```

### Migrations

```bash
# Applied and verified against the shared test database:
NEMSEI_V2_DATABASE_URL=postgresql+psycopg://nemsei:nemsei-test@127.0.0.1:55432/nemsei_v2_test \
  python -m alembic upgrade head        # -> head is now 0039_availability_daily
NEMSEI_V2_DATABASE_URL=postgresql+psycopg://nemsei:nemsei-test@127.0.0.1:55432/nemsei_v2_test \
  python -m alembic check               # -> "No new upgrade operations detected."
```

**Not yet run against the production `nemsei_v2` database.** That is a
separate, explicit deploy action (§8 below), not implied by this session's
work.

### Commands available now (none of them touch a provider API)

```bash
# Backfill/repair a date range from already-collected facts:
docker exec nemsei-v2-worker-1 python /app/scripts/v2_availability_materialize.py \
    --asset-id <v2-asset-id> --from 2026-08-25 --to 2026-09-03

# Read-only comparison against V1's real historical data (writes nothing):
docker exec nemsei-v2-worker-1 python /app/scripts/v2_availability_compare.py \
    --asset-id <v2-asset-id> --v1-asset-id <confirmed-v1-asset-id> \
    --from <date> --to <date>
```

### Gaps that still block turning this on in production

1. **Migration not applied to the production database yet.** `alembic
   upgrade head` above was run against the test database only.
2. **Not run against asset 153's own live data yet.** The materializer and
   compare script are built and DB-tested, but `v2_availability_materialize.py`
   / `v2_availability_compare.py` have not been executed against the real
   `nemsei_v2` production database — that requires production DB
   credentials this session was not given and should not assume, per §7's
   own human-sign-off requirement. This is the concrete next step, not a
   new build.
3. **Reporting/Excel/UI integration (item 6) is not wired.** The exact four
   edit points are identified (§8 above: `assembler.py`'s
   `AVAILABILITY_FIELDS_WITHOUT_SOURCE`/`include_availability_kpi`,
   `datasets.py`'s `DATASET_METRICS`, the Excel builders, and
   `portfolios/availability.html`'s placeholder) but deliberately left
   untouched pending the asset-153 validation run in gap 2 and a human's
   explicit go-ahead, exactly as §7 specifies.
4. **The `deduplicate_observed_at` caveat is real and unverified against a
   full live day** (`availability_service.py`'s own docstring): a device
   stuck reporting one unchanging nonzero value for over 90 minutes would
   not gain a new `device_status_facts` row and could misread as a gap that
   never happened. The 390-day real-data comparison above validates the
   *algorithm* against V1's historical data (which has this same property,
   since V1's own polling had gaps too); it does not by itself prove V2's
   *live* 30-minute-cadence collection (asset 153, running since
   2026-08-25) is dense enough end-to-end for a full daylight window — that
   is exactly what running `v2_availability_compare.py` against asset 153's
   own live data (gap 2) would settle.
5. **Concurrent uncommitted session (§0)** — still uncommitted as of this
   writing. This session's new files did not touch any file in that diff,
   but a migration is still schema-global: confirm `0039`'s `down_revision`
   (`0038_work_order_priority`) is still the true head before deploying, in
   case the other session lands a migration first.
6. **Sigenergy stays blocked by design** (item 9) — proven structurally by
   `test_sigenergy_asset_has_no_expected_devices`, not by a special case to
   revisit later.

## Addendum, 2026-09-06: contractual vs operational, scheduling, retention

A later session re-verified the checkout from scratch (nothing assumed from
the sections above — the V1 constants, the monthly semantics and the
migration head were all re-read from source) and closed the gaps that the
2026-09-04 work had left. The engine, the two daily tables and the reporting
wiring described above were all found present and green (74 tests) before any
change was made; what follows is only what was added on top.

### The gap that mattered most

Everything above computes **one** kind of availability — a port of V1's
*sampled* engine — and stored it in a column called `availability_pct` that
the customer PDF renders with no qualifier. V1 kept its two engines apart
only by *which table a caller happened to read*, and V2 had inherited that:
`AVAILABILITY_SOURCES = ("fusionsolar_sampled",)`, no source on the report
payload, no policy. A realtime-derived operational estimate was therefore one
`SELECT` away from being read as a warranted contractual figure — the exact
substitution this milestone's brief forbids.

### What was added

- **`reporting/rules/availability_source.py`** (new): the source vocabulary
  (`fusionsolar_sampled`, `provider_wat`, `provider_device_availability`,
  `manual`), the `contractual`/`operational` classification, and
  `select_availability` — contractual outranks operational **always**, never
  by recency, with deterministic tie-breaking so assembly order cannot change
  a report.
- **`source_kind` on both daily tables** (`0041_availability_source_kind`),
  with a CHECK enumerating the valid `(source, source_kind)` pairs. The
  database itself refuses to store `fusionsolar_sampled` as `contractual`;
  the split is structural, not a convention a writer can forget.
- **The uniqueness key now includes `source`** (same migration). Previously
  `UNIQUE(asset_id, availability_date)` allowed only one row per day
  regardless of source, which made "prefer contractual over operational"
  *unreachable* — the two could never coexist to be chosen between. The
  materializer's delete-and-reinsert is scoped to its own source for the same
  reason: an unscoped delete would have made re-running the sampled engine
  silently destroy a contractual row.
- **Monthly aggregation is now per-source, with each source's own V1
  semantics.** This was investigated rather than assumed, and V1 turns out to
  aggregate its two engines *differently*:
  - operational → arithmetic mean over days, gated on every day being
    `complete` (V1 `sampled_month_quality`) — unchanged, still golden;
  - contractual → `SUM(pct × valid_slots) / SUM(valid_slots)`, evidence-
    weighted (V1 `get_monthly_availability`, `reporting/repositories.py` —
    the query that actually fed V1's reports and monthly close).
  Applying the arithmetic mean to a contractual source would silently change
  a commercial number, so the branch exists before any contractual source
  does. Both are pinned by test.
- **`availability_source`/`availability_source_kind` on
  `reporting_dataset_rows`** (`0042_dataset_availability_source`) and through
  the assembler payload. A period whose measured months disagree on source
  reports `partial` and withholds the percentage rather than labelling a mean
  of a warranted and an operational month as either.
- **Scheduled materialization** (`availability.materialize`): off by default,
  provider-free, hourly, and — this is the whole recompute policy —
  **only the trailing `lookback_days` window** (3 by default), never the full
  history. Old days move only through the explicit backfill script.
- **`materialize_availability_window`**: a batched fleet-wide pass, three
  reads for the whole (assets × days) window instead of two per asset-day.
  The scheduled job and the backfill CLI share it; the query count is
  asserted by test, not by comment.
- **`assets.service.retire_device`**: `Device.valid_to` existed and the
  availability query already honoured it, but **nothing could write it** —
  there was no supported way to retire an inverter without deleting it, which
  would have rewritten every historical figure it contributed to. Closes the
  device's window and its device-scoped mappings together, idempotently.
- **`days_with_facts_but_no_availability`**: the retention precondition.
  Nothing purges `device_status_facts` today, so it guards nothing yet; it
  exists so the ordering requirement (materialize first, delete second) is
  ready rather than rediscovered.
- **UI**: one column on the portfolio availability page — *Contratual* vs
  *Operacional (amostrada)* — so an operator can tell the two apart without
  opening the database. No redesign.

### What still cannot be done honestly

- **No provider gives V2 a contractual WAT today.** V2's FusionSolar client
  has `getDevList` and `getDevRealKpi` only. V1's contractual/slot engine was
  fed by `/thirdData/getDevHistoryKpi` (`monitoring_board/constants.py`),
  which V2 does not implement — a second API surface against a shared,
  rate-limited account. `provider_wat`/`provider_device_availability` are
  therefore registered and testable but **unpopulated**: the plumbing is
  ready, the data is not. Nothing in the code fabricates one.
- **Sigenergy stays structurally blocked** — unchanged, still proven by
  `test_sigenergy_asset_has_no_expected_devices`.
- **The `deduplicate_observed_at` caveat is still open and still unverified
  against a full live day** — see the 2026-09-04 addendum's gap 4. Unchanged
  by this session.
- **Still not run against production.** Both migrations are verified against
  an empty database, a populated one, and a downgrade/re-upgrade cycle, but
  `alembic upgrade head` has not been run on `nemsei_v2` itself, and no
  availability has been materialized from asset 153's real live data.

## Addendum, 2026-09-06 (later): contractual WAT is now a real source

The gap the previous addendum closed with a plumbing-only answer
(`provider_wat`/`provider_device_availability` registered but unpopulated)
is now closed with data. `/thirdData/getDevHistoryKpi` was probed live
against the real account and works; V1's slot engine is ported and matches
V1 on real data; ingestion, scheduling, backfill and reporting selection are
built and tested.

Full write-up — endpoint shape, rate limits, semantics, the two deliberate
divergences from V1, the real-data comparison, and the remaining limits —
lives in **`docs/v2/FUSIONSOLAR_DEVICE_HISTORY.md`**. The short version:

- Source `fusionsolar_device_history`, kind `contractual`. Deliberately *not*
  named `provider_wat`: FusionSolar publishes no availability figure, so this
  is derived from power history exactly as V1's contractual engine was.
- Contractual and sampled coexist per day and the selection policy already
  built here picks contractual, unchanged.
- Contractual months are slot-weighted (`SUM(pct × valid_slots) / SUM(valid_slots)`);
  operational months keep V1's flat day mean. The branch is on `source_kind`.
- Real-data parity against V1's only contractual day (2026-07-24): 312/319
  device-days and 123/130 plant-days exact, `valid_slots` 130/130 exact. All
  7 divergences are one intentional rule — V1 published `0.0%` for inverters
  that returned **no data at all**; V2 reports `None`.
- The sampled engine's near-universal `partial` (3 of 6 720 device-days ever
  complete) is now explained with numbers: V1's realtime poll produced 1-6
  samples per device-day and 68.6% of consecutive gaps exceeded 90 minutes.
  It is a density problem, not a threshold problem — `late_first_sample` and
  `early_last_sample` account for 44 warnings between them, against 6 030
  gap and 5 813 minimum-count warnings. Thresholds were left untouched.
