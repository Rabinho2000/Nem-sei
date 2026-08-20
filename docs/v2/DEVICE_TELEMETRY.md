# Device-level live telemetry: contract audit, design, and Fatia 2 (M7)

This document is the audit requested before any implementation, followed by
what that audit produced. [`DIAGNOSTICS.md`](DIAGNOSTICS.md) built Fatia 1
(V1-imported device history, `device_status_facts`, migration `0015`) and
named Fatia 2 -- a live device-level read -- as deliberately not built, gated
on a verified per-provider contract, the same gate production reads already
had. This is that audit, and the code it produced.

## 1. Contract audit: what each provider can actually return

### FusionSolar: a real, V1-proven device contract

V1 never guessed at this. `monitoring_board/services/fusionsolar_client.py`
has working, still-running code (`fetch_device_list`, `fetch_device_realtime_map`)
that produced every one of the 51 289 rows Fatia 1 imported:

| Step | Endpoint | Payload | Response |
| --- | --- | --- | --- |
| Device inventory | `POST /thirdData/getDevList` | `stationCodes` (≤100) | Device rows: `dev_type_id`, `external_device_id`/`devDn`/`sn` |
| Device current KPI | `POST /thirdData/getDevRealKpi` | `devIds` (≤100, grouped by type) + `devTypeId` | `dataItemMap` with `inverter_state`, `active_power`, `day_cap`/`dayEnergy` |

`dev_type_id ∈ {1, 38}` is V1's own verbatim inverter vocabulary
(`FUSIONSOLAR_INVERTER_DEVICE_TYPE_IDS`); every one of the 325 V1 devices
carries one of these two values, confirmed directly against the live V1
database (`provider_devices`: 325 rows, all `provider='FusionSolar'`; 0 rows
for any other provider).

Per the four requested signals:

| Signal | Verified? | Evidence |
| --- | --- | --- |
| Inverter state | **Yes** | Same field V1 read 51 289 times; classifier already ported (`diagnostics/rules.py`, Fatia 1), pinned against every real code V1 ever saw |
| Active power | **Partially** | Field name proven (`active_power`); **unit is not proven** -- see §1.3 |
| Day energy | **Partially** | Field name proven (`day_cap`/`dayEnergy`); assumed kWh by analogy to the verified `PVYield` daily contract, not independently confirmed for this endpoint |
| Last communication | **Open question** | See §1.4 -- this is the one signal the canary must resolve, not just confirm |

MPPT/string (`pv{n}_i`/`pv{n}_u`) live in this exact same `dataItemMap` and
were proven empty in all 51 289 V1 rows (Fatia 1). Not modeled here, per the
milestone's own instruction -- and structurally impossible to leak, since the
normalizer in `device_status.py` never reads those keys at all.

Alarms (`getAlarmList`) are a distinct, unaudited contract; `telegram_alerts`
is V1's own send log, not a provider feed. Out of scope for this slice, as
`DIAGNOSTICS.md` already said.

#### 1.1 A real defect in V1's code, not ported

`normalize_power_to_kw` (`monitoring_board/services/installation_import.py`):

```python
def normalize_power_to_kw(value):
    parsed = float_value(value)
    return parsed / 1000 if parsed > 1000 else parsed
```

This is a magnitude guess, not a verified unit -- exactly the class of thing
`FusionSolarProductionContract` was built to refuse for `PVYield`. It is not
ported. `device_status.py`'s `FusionSolarDeviceContract` instead requires an
operator to declare `NEMSEI_V2_FUSIONSOLAR_<REF>_DEVICE_POWER_UNIT` (`kW` or
`W`) explicitly before any HTTP request; a missing declaration is a safe
configuration failure, never a silently mis-scaled reading. `_DEVICE_ENERGY_UNIT`
is required as literal `kWh`, mirroring `PRODUCTION_UNIT`'s strictness.

#### 1.2 A real bug in V1's code, not ported: freshness defaulted to true

`app_factory.py`'s realtime sync:

```python
has_recent_data = True if realtime and seen_dt is None else bool(
    seen_dt and collected_at_dt - seen_dt <= timedelta(minutes=...)
)
```

When the response carries no parseable timestamp, V1 silently assumed the
reading was recent. This explains why `communication_status` reads `"recent"`
on every one of 51 289 stored rows without exception (Fatia 1) -- it is very
plausibly not that FusionSolar was always fresh, but that V1 never actually
tested the branch where it wasn't. `device_status.py` inverts the default: no
provider timestamp means `freshness="unknown"`, never `"fresh"`. This mirrors
the existing plant-level precedent in `monitoring.py` ("the verified
current-state response has no reliable provider observation time... it never
interprets stale data as offline") rather than inventing a new rule.

#### 1.3 Open question the canary must answer, not just confirm

Whether `getDevRealKpi` rows ever actually carry a usable `collectTime` is
**not established** by any V1 evidence available to this audit -- V1's own
code checked for one defensively but its "recent" default masks whether the
field was ever really populated. `docs/apis/fusionsolar.md` carries the same
unresolved `collectTime` TODO for the neighbouring `device_history_kpi`
endpoint. This is the single most important thing the live canary (§4) is
for: not to re-confirm state/power/energy (already well evidenced), but to
determine, for the first time, whether "última comunicação" can be pinned
tighter than "during this sync run".

### Sigenergy: no device-level contract exists, anywhere, in any evidence

This was checked exhaustively, not assumed absent from a quick read:

- **V2's own client** (`integrations/sigenergy/client.py`) implements exactly
  three calls: `authenticate`, `discover_systems`, `get_energy_flow`. All
  three are **system-level** (a Sigenergy "system" = one whole plant's PV +
  battery + inverter combination as a single account-visible unit). There is
  no device list, no device KPI, no device identifier anywhere in the client.
- **V1's own Sigenergy integration** is more mature than V2's (energy history,
  onboarding, preview worker) and its own documentation
  (`Nem-sei/docs/apis/sigenergy.md`) states plainly: *"Não existem endpoints
  implementados para: alarmes; inversores; strings; availability; controlo
  remoto."* V1 never called a Sigenergy inverter endpoint, ever, in
  production.
- **The V1 database confirms it structurally**, not just by absence of code:
  `provider_devices` has 325 rows and every one is `provider='FusionSolar'`.
  `device_realtime_snapshots` has 51 289 rows, all `provider='FusionSolar'`.
  Zero Sigenergy rows exist in either table. There has never been a Sigenergy
  device identity to poll, an endpoint to poll it with, or a single historical
  reading to learn a contract from.

**Conclusion: Sigenergy device-level telemetry is not a gap this milestone
can close.** It is not unverified -- it is unevidenced. Closing it would
require either a real Sigenergy device/inverter endpoint that no evidence
anywhere in this codebase or V1's history has ever exercised (a genuinely new
provider capability, not a Fatia 2 extension), or redefining "device" as "the
whole system" and reusing the existing plant-level `energyFlow` fields
(`pvPower`, `batteryPower`, `batterySoc`, `loadPower`) under a device-shaped
label -- which would not add information already captured by
`MonitoringObservation` at the mapping level, and would misrepresent a plant
as a device. Neither is built. `providers/registry.py` states this
structurally: Sigenergy's `implemented_capabilities` does not include
`DEVICE_MONITORING`/`DEVICE_DISCOVERY`, with the evidence above as its
comment, so `evaluate_capability` reports it `UNSUPPORTED` rather than
silently `UNKNOWN`.

## 2. What was built

Scoped to FusionSolar only, per §1.

- **`device_status.py`** (`integrations/fusionsolar/`): `FusionSolarDeviceContract`
  (verified power/energy units, §1.1), row normalizers for both endpoints, and
  `FusionSolarDeviceStatusService.sync_device_status(connection_id)` --
  authenticate once, `getDevList` per plant to learn `dev_type_id` (filtered
  to the verified inverter set), `getDevRealKpi` batched by type, normalize,
  persist.
- **`client.py`**: `device_list_batch` and `device_current_monitoring_batch`,
  narrow HTTP methods mirroring the existing plant-level ones exactly (same
  validation, same 100-item batch ceiling, same error mapping).
- **Migration `0016`**: `device_status_facts` gains `freshness`/`quality`/
  `completeness` (same vocabulary as `monitoring_observations`, first-class
  columns rather than buried JSON) and `sync_run_id` (nullable, traces a live
  fact back to its `SyncRun` and `ProviderRequestAttempt` rows). Every
  existing Fatia 1 row defaults to `unknown` on all three -- V1 recorded
  neither, so a computed value would be invented, not imported.
  `record_device_status` gained matching parameters and a
  `deduplicate_observed_at` flag mirroring `confirm_current_monitoring`'s: a
  live reading with no independent provider timestamp always writes
  `observed_at=ingested_at`, which differs on every poll by construction, so
  without deduplicating on it an unchanged device would mint a new revision
  at every single poll, forever.
- **Selection**: a new `current_device_mappings_for_connection` repository
  method (device-scoped mirror of the existing plant one). Device selection
  does **not** go through `resolve_source_policy` -- `SOURCE_USES` only
  models `monitoring`/`production`, both questions of *which connection* is
  authoritative for a plant when more than one could claim it. A device claim
  always carries its own `device_id`
  (`ck_asset_provider_mappings_device_link`), and the partial unique index on
  `(provider_connection_id, resource_kind, normalized_external_id)` already
  guarantees at most one active connection can claim a given device --
  there is no competing-source question left for a device to resolve.
- **`providers/registry.py`**: FusionSolar's `implemented_capabilities` gains
  `DEVICE_DISCOVERY`/`DEVICE_MONITORING` (already-existing enum members that
  had no implementation behind them until now). "Implemented" means the code
  path exists behind its own verified-contract gate, exactly like
  `PRODUCTION_HISTORY` -- and, as of §5, it has now also been proven live, not
  just against fakes. Sigenergy's set is unchanged, with the §1 evidence
  recorded in-line as the reason.
- **Tests**: `tests_v2/test_fusionsolar_device_status.py` -- contract
  enforcement (missing/unverified units refuse with zero provider calls),
  unit scaling (W→kW by verified contract, not by magnitude), MPPT/string
  fields never read even when present in the fixture payload, freshness
  derived honestly (no `collectTime` ⇒ `"unknown"`, never `"fresh"`; a
  regression guard against reintroducing V1's bug), idempotency (identical
  repeated reads produce no new revision; a changed reading does, with
  `supersedes_fact_id` linking them), and unverified device types (e.g.
  `dev_type_id=17`) never persisted.

All existing V2 tests continue to pass; one existing test
(`test_fusionsolar_slice.py`'s capability-narrowness pin) was updated to
include the two newly-implemented capabilities, with a comment explaining why.

## 3. Persistence: provenance, freshness, quality, idempotency

Every live fact written by `sync_device_status` carries:

- **Timestamps**: `observed_at` (the provider's own `collectTime` when
  present and parseable; otherwise this run's own `ingested_at`) and
  `ingested_at` (always, from `record_device_status`).
- **Freshness**: `"fresh"` (collectTime within 3h of ingestion), `"stale"`
  (older), or `"unknown"` (no usable collectTime) -- never defaulted to
  `"fresh"` (§1.2).
- **Quality/completeness**: `"complete"` when state, power and energy are all
  present in the response; `"partial"` when some are; `"missing"` when none
  are -- mirroring `MonitoringObservation`'s vocabulary exactly.
- **Provenance**: `source_kind="live_read"`, `sync_run_id` linking to the
  `SyncRun` and its `ProviderRequestAttempt` rows, plus `raw_inverter_state`
  and `observed_at_source` in `metadata_json`.
- **Idempotency**: the existing `(device_id, source_fact_key, source_revision)`
  unique constraint plus `deduplicate_observed_at` (§2) mean an unchanged
  reading, however many times it is re-polled, produces zero new rows. A
  changed reading always produces a new, superseding revision -- append-only,
  auditable, never an overwrite.

## 4. Polling cadence: designed against real V1 evidence, not a guess

### What V1's own cadence actually was, and why it produced sparse data

This was measured directly against the live V1 database, not assumed:

- **51 289 rows across 325 devices, 2026-05-18 to 2026-07-13** (~57 days):
  ≈2.8 samples/device/day on average, but the *distribution* matters more
  than the mean -- most device-days carry 1-6 readings, clustered in bursts a
  few minutes apart followed by gaps of many hours (directly observed:
  device 1's samples on 2026-05-18 land at 10:13, 10:30, 10:37, 10:48 ×2,
  11:00, then nothing until 12:54).
- **This is not accidental under-sampling; it is the documented design.**
  Device-level realtime is not on its own schedule at all. It piggybacks
  opportunistically on certain plant-monitoring sync triggers
  (`app_factory.py`: only runs when `trigger_type not in {"scheduled_state",
  "manual_background"}`), and even then shares a small, low-priority daily
  budget: `docs/apis/fusionsolar.md` documents `FUSIONSOLAR_WAT_DAILY_BUDGET=36`
  calls/day for the "WAT/diagnostics" area, explicitly **last** in FusionSolar's
  own account-wide priority order (production, month-close, backfill, reports,
  *then* WAT/diagnostics), processing at most 10 inverters per call.
- This is exactly why weighted availability computed no value from this
  history (`DIAGNOSTICS.md`): a 90-minute-gap completeness rule cannot survive
  hours-long gaps that are the system working as designed, not a fault.

### The cadence this milestone proposes

A **dedicated, deliberate** poll, decoupled from V1's opportunistic pattern,
sized to actually satisfy `sampled_availability.py`'s 90-minute-gap rule
without contending with V1's shared budget:

| Parameter | Proposed value | Reasoning |
| --- | --- | --- |
| Poll interval | Every 30-45 minutes during a plant's local daylight window | Comfortably inside the 90-minute gap tolerance even allowing for one missed cycle |
| Batch shape | `getDevList` once per connection per day (inventory is stable); `getDevRealKpi` per poll, batched 100 devices/call, grouped by `dev_type_id` | Mirrors V1's own batch ceilings exactly; device typing rarely changes |
| Calls/device/day (30 canary devices, one plant) | ~1 discovery call/day + ~20-28 KPI calls/day (daylight hours ÷ interval) across the whole account, not per device -- batching means one call covers up to 100 devices | Far under any budget V1 ever needed for a single-plant canary |
| Scale-out cost | With FusionSolar's ≤100-ID batch ceiling, every additional 100 devices adds one more call per poll cycle, not one call per device | Linear and small; the real constraint is account contention (below), not call volume |

**This cadence is proposed, not scheduled.** No job registration, cron entry,
or scheduler wiring was added in this slice -- `sync_device_status` exists as
a callable service, exactly as `FusionSolarProductionService`/
`FusionSolarMonitoringService` did before their own schedulers were wired.
Turning the cadence on is a deliberate next step, not a side effect of this
one.

### The account-contention constraint this cadence must still respect

`docs/v2/FUSIONSOLAR_CANARY.md` (M4) already established that V2 has no
FusionSolar account of its own -- it shares V1's, and a `failCode=407` cools
down the *entire account*, deferring every V1 job. The M4 canary was only
possible with V1's FusionSolar activity paused for three short windows. That
constraint is unchanged and applies identically here: **this cadence must not
run continuously against the shared account.** It is safe to validate once
(§5, same discipline as M4) and then must stay off until either V2 has its own
account or an operator explicitly accepts the shared-account risk for a wider
rollout.

## 5. Canary: executed live, 2026-08-20

Run against the real, production FusionSolar account (shared with V1, same
account M4 used), on the real production V2 database
(`nemsei_v2`), through the already-running `nemsei-v2-web-1` container so the
app's own resolved database/credential handling was used throughout --
nothing was read or persisted through a side channel.

**Canary chosen: a multi-inverter asset, per this milestone's own
instruction** (M4 used a single-inverter plant; this slice specifically needed
to prove `getDevRealKpi`'s `devTypeId` grouping across more than one device).
`Queijaria Lourenço & Filhos (EPC)`, asset 153, FusionSolar plant
`NE=154743789`, 36.05 kWp, **two inverters** (33.0 kW and 30.0 kW,
`dev_type_id=1` both), chosen as the smallest multi-inverter asset in the
portfolio by installed power -- the same "small, unimportant, self-consistent"
selection logic M4 used, just filtered to `device_count between 2 and 5`.

**Safety procedure, identical in kind to M4:**

- Pre-flight: verified backup of the real `nemsei_v2` database (`pg_dump -Fc`,
  6.2 MB, magic-header verified) taken *before* migration `0016` was applied.
- Migration `0016` applied to the live database (`alembic upgrade head`);
  confirmed after: all 51 289 existing rows carry `unknown`/`unknown`/`unknown`
  on the three new columns, exactly as designed -- nothing was guessed
  retroactively.
- One plant mapping + two device mappings created as `active` on the existing
  `fusion-canary` connection (id 3, left `enabled`/`configured` since M4;
  `credential_reference='primary'`) -- the same kind of admin action M4's own
  single mapping was.
- **Hard call limit**: no retries configured (`max_transient_retries=0`); the
  service's own batching (one auth + one `getDevList` + one `getDevRealKpi`
  per distinct `dev_type_id`) is the only source of calls. Total across both
  windows below: **5 real provider requests.**
- **V1 paused (`docker pause`, a cgroup freeze -- zero writes to V1's database,
  fully honoring "V1 read-only") for the live windows only**, resumed
  (`docker unpause`) immediately after, guarded by a shell `trap` so a crash
  mid-script could not leave V1 paused. Verified after each window that V1's
  own container returned to `running` and its web port answered a normal
  request.
- **Stopped at the first anomaly.** See below.

**Window 1 (2.14 s of V1 downtime): a real anomaly, caught and not persisted.**
Auth and `getDevList` both succeeded against the live account (3 calls
counted, 2 real HTTP requests -- auth + discovery), but `sync_device_status`
matched **zero** of the two expected devices and persisted nothing
(`status=partial`, `accepted=0`, `rejected=2`). Per this milestone's own
instruction ("para à primeira anomalia"), nothing was retried blindly. Root
cause, found by reading the live response shape against V1's own device
mappings: `normalize_device_type_row` read the identity fields in the wrong
priority order (`devDn` before `devId`/`id`), so it never matched the
`external_device_id` value V1's own `provider_devices` import (and this
slice's own mappings) are keyed on. Fixed to the exact precedence V1's
`normalize_fusionsolar_device_identity` already proved
(`devId, id, devDn, deviceDn, esnCode, sn`) -- ported verbatim, per this
module's own stated discipline, not re-guessed. All 8 unit tests re-verified
green after the fix; V1 was already resumed (confirmed `running`) before the
fix was even written.

**Window 2 (3.49 s of V1 downtime): success.**

```
status: success   completeness: complete
provider calls: 3 (auth, getDevList, getDevRealKpi[devTypeId=1])
device 289 (33.0 kW): available, active_power_kw=6.663, day_energy_kwh=17.860
device 290 (30.0 kW): available, active_power_kw=8.867, day_energy_kwh=21.030
raw_inverter_state=512.0 for both -> classified "available" (matches Fatia 1's ported classifier)
freshness=unknown for both -- see §1.3, now resolved below
```

**Total V1 downtime across both windows: 5.63 seconds** (versus M4's 3 min 24
s across three windows) -- shorter because this canary never needed to probe
an undocumented day-window contract; the anomaly it hit was caught and fixed
without a second live call being wasted on it.

### §1.3 resolved: `collectTime` is confirmed absent from `getDevRealKpi`

Not "still open" -- **checked, and the answer is no.** Neither device's
response row carried a parseable `collectTime`/`collectedAt` field;
`normalize_device_realtime_row` correctly fell back to
`freshness="unknown"`, `observed_at=ingested_at`,
`metadata.observed_at_source="ingested_at_no_provider_timestamp"` for both --
never silently defaulted to `"fresh"` the way V1's own code did. This is now
the best available explanation, checked rather than assumed, for why V1's own
`communication_status` read `"recent"` on all 51 289 historical rows without
exception (§1.2): the field this account's `getDevRealKpi` response would need
to justify that label was never actually present to check.

### Active power and day energy: unit confirmed by magnitude, same method M4 used for `PVYield`

No independent unit-labelled field exists in the response (as expected, §1.1),
so the same evidentiary method `FUSIONSOLAR_CANARY.md` used to confirm
`PVYield=kWh` applies here: compare the persisted value's magnitude against
known plausible bounds. `NEMSEI_V2_FUSIONSOLAR_PRIMARY_DEVICE_POWER_UNIT=kW`
was set *provisionally*, matching the scale of V1's own last stored readings
for these exact two inverters (`device_realtime_snapshots`,
2026-07-12T17:00: 5.342 kW / 4.959 kW on the same 33/30 kW-rated units). The
live result -- **6.663 kW / 8.867 kW**, same order of magnitude, same
per-inverter ranking pattern -- confirms `kW` is correct: had the raw value
actually been Watts, the persisted numbers would have read ~6663/~8867 kW,
absurd for a 33/30 kW inverter, and would have been visibly wrong on
inspection. Day energy tells the same story: **17.860 / 21.030 kWh**
accumulated by 09:21 UTC (≈10:21 Lisbon) is proportionate to V1's own
135.78/144.18 kWh *end-of-day* baseline for the same inverters -- a plausible
fraction of a full day's yield at mid-morning, not a value consistent with Wh
(would read ~17860) or MWh (would read ~0.018). `kWh` is confirmed.

**One honest limit, stated plainly:** this is a magnitude-plausibility
confirmation from a single reading, exactly the class of evidence M4 itself
relied on for `PVYield` -- not an independent unit field in the payload. It
is the strongest evidence available without one, and it is not weaker than
what M4 already established the portfolio's production numbers on.

## 6. Availability: still not turned on, and still correctly so

`weighted_sampled_availability` (`rules/availability.py`) and
`sampled_availability.py`'s port remain exactly where `DIAGNOSTICS.md` left
them: correct, ready, and **not wired**, because V1's own history could never
feed them densely enough (3 of 6 720 device-days, `DIAGNOSTICS.md`). The
canary (§5) does not change that, and could not by design: **it is one
reading, once**, not a sustained poll -- deliberately, since running the
proposed cadence for real right now would itself be "escalar", which this
milestone was explicitly told not to do yet. One point cannot satisfy a
90-minute-gap completeness rule any more than V1's own 1-6-samples-a-day
history could (`DIAGNOSTICS.md`).

What the canary *does* establish, that a design document alone could not: the
**real cost** of one full cycle is 3 provider calls for a 2-device,
single-`dev_type_id` plant (1 auth + 1 discovery + 1 KPI batch), completing in low
single-digit seconds end-to-end. Projected against the proposed §4 cadence
(one poll every 30-45 minutes, `getDevList` cached per day), that is cheap
enough for this canary's own scope to run indefinitely without stressing the
shared account -- but "cheap for one asset" is not "cheap for 135 plants /
325 devices", and the account-contention constraint (§4, unchanged since M4)
still gates that scale-up. Sustained density -- the actual precondition for
turning availability on -- requires the cadence to actually run for multiple
consecutive days and be checked against `sampled_availability.py`'s own
completeness rules. That is the next concrete step, not a new study, and it
was deliberately not started here.

## 7. Summary: what was asked, what this answers

- **Signals verified per provider, live, 2026-08-20** (asset 153, 2 inverters,
  §5):
  - Inverter state: **VERIFIED** (already ported in Fatia 1; live
    `raw_inverter_state=512` classified `available` for both devices, matching
    V1's own vocabulary).
  - Active power (field + unit): **VERIFIED** -- `active_power` field, `kW`
    unit confirmed by magnitude against V1's own stored baseline for the same
    two inverters (§5).
  - Day energy (field + unit): **VERIFIED** -- `day_cap` field, `kWh` unit
    confirmed the same way (§5).
  - Last communication / `collectTime`: **VERIFIED -- confirmed absent.** Not
    an open question any more (§1.3, resolved in §5): the live response
    carries no usable timestamp for this account, so "última comunicação" is
    answered by the read's own `observed_at`/`ingested_at`, never by a
    provider-stated instant.
  - MPPT/strings: **BLOCKED** by design -- never queried, per this milestone's
    own scope, not because of a technical failure.
  - Alarms: **BLOCKED** -- out of scope for this slice, needs its own
    verified contract (`getAlarmList`, unaudited).
  - Sigenergy (all four signals): **BLOCKED** -- no contract exists to verify
    against; V1 never called a Sigenergy device endpoint, ever (§1, Sigenergy).
    Unchanged by this canary, on purpose.
- **Calls/cadence needed**: measured, not estimated -- 3 real calls for one
  2-inverter, single-type plant, low single-digit seconds. Proposed cadence
  (30-45 min, §4) is realistic **at this canary's own scale**; realistic *for
  the portfolio* remains gated on the same shared-account constraint M4 already
  identified, not on anything this canary found wrong.
- **Is density sufficient for availability now**: no, and this canary could
  not have made it sufficient by design -- one reading is not a sustained
  poll. Availability stays off (§6).
- **Gaps still open**: (1) sustained multi-day density has not been measured
  -- the cadence has not been scheduled or left running, deliberately, per
  "não escalar ainda à carteira"; (2) Sigenergy device telemetry has no
  contract to build on at all (§1, Sigenergy) -- a real gap, not a to-do;
  (3) V2 still shares one FusionSolar account with V1, so any cadence beyond
  this single canary asset needs either a dedicated V2 account or an
  operator's explicit acceptance of that contention (§4); (4) scheduler wiring
  for the proposed cadence does not exist yet, by design -- turning it on is a
  distinct, later decision.
