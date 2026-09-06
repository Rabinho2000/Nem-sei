# FusionSolar device history: the contractual availability source

Status: **endpoint verified live against the real account, 2026-09-06.**
Ingestion, calculation, scheduling, backfill and reporting integration are
built and tested; nothing has been enabled in production
(`availability_history_sync_enabled` defaults to `False`).

## 1. The endpoint, as it actually behaves today

Not assumed from V1 — probed live, read-only, from the production worker's
own egress path.

```
POST /thirdData/getDevHistoryKpi
{"devIds": "<comma-joined, max 10>", "devTypeId": <int>, "startTime": <ms>, "endTime": <ms>}
```

Verified response (asset 153, station `NE=154743789`, two inverters,
2026-09-04):

- HTTP 200, `{"success": true, "failCode": 0, "message": null, "data": [...]}`
- `data` is a **flat list**, not paginated: 558 rows for 2 devices × 1 day.
- Row shape: `{devId, sn, collectTime, dataItemMap}`.
- **5-minute granularity**, 00:00 → 23:55 (285 and 273 rows; a perfect day
  would be 288). Observed step gaps: mostly 300 s, occasionally 600/900/2700 s
  — the provider itself drops readings.
- `dataItemMap` carries ~100 keys including `active_power`, **`inverter_state`**,
  `day_cap`, `total_cap`, `efficiency`, `temperature`, `elec_freq`,
  `open_time`/`close_time`, and per-string `pvN_i`/`pvN_u`.
- `inverter_state` values seen: `512.0` (running), `40960.0` (standby), `2.0`,
  `0.0`, and `null`.
- `collectTime` is epoch milliseconds, aligned to the requested window.

**Two things V1 never used and V2 now stores anyway**: `inverter_state` (V1's
contractual engine looked only at `active_power`) and the full row provenance.
The availability *calculation* still uses power alone, deliberately — see §3.

### What the provider does *not* give

FusionSolar publishes **no availability figure and no WAT** anywhere on this
API. Both V1 and V2 *derive* availability from power history. That is why the
source is named `fusionsolar_device_history` and not `provider_wat` /
`provider_device_availability` — those names stay reserved for a provider that
states the number itself, and none that V2 talks to does.

### Timezone

V1 built this window in the **calling process's local timezone**
(`closed_day_window_ms`), an implicit contract that changed meaning with the
container's `TZ`. V2 requires it explicitly per connection:
`NEMSEI_V2_FUSIONSOLAR_<REF>_DEVICE_HISTORY_TIMEZONE`. A missing value is a
configuration error, never a guess, because it shifts every boundary day's
percentage. DST days are 23 or 25 hours wide, asserted by test.

## 2. Rate limiting and coordination

V1 had a dedicated `WAT_HISTORY_AREA` and `FUSIONSOLAR_WAT_DAILY_BUDGET = 36`.
**V2 has no such names, and none were added**: V2 expresses exactly that idea
as a row in `provider_request_states` keyed by `endpoint_family`. This
integration adds `endpoint_family="device_history"`, so it gets its own call
counter, `next_allowed_at` and 407 `cooldown_until` for free, and goes through
the same `FusionSolarRequestController` — ownership lease, session cache,
transient retry — as every other FusionSolar read. No parallel quota exists.

Measured call cost (one `getDevList` per 100 stations + one history call per
10 inverters, per day):

| fleet | 1 day | 30 days | 1 year |
|---|---|---|---|
| V1's real fleet (130 plants / 319 inverters) | **34** | 1 020 | 12 410 |
| ~250 assets at the same density (613 inverters) | **65** | 1 950 | 23 725 |
| pessimistic 250 × 8 inverters | 203 | 6 090 | 74 095 |

The first row is the check that the model is right: V1's own budget for this
endpoint was 36 calls/day, and the real fleet needs 34.

## 3. Semantics

`availability_slots.py` ports V1's **slot** engine verbatim in constants and
shape:

- 15-minute slots (`SLOT_MINUTES`), 30-minute edge tolerance
  (`EDGE_TOLERANCE_MINUTES`) trimmed off each day's own observed range.
- A slot is "producing" iff `active_power > 0` — power only. `inverter_state`
  is persisted but not consulted, because redefining availability on it would
  silently move every customer's historical percentage. Changing that is a
  deliberate, separate decision the stored facts already make possible without
  a new provider call.
- Plant `valid_slots` = the *tolerated* union of slots where any expected
  inverter produced. This is V1's `plant_availability_daily.valid_slots` and
  the exact weight its monthly query uses.
- Device availability = |device's producing slots ∩ considered| / |considered|.

### Two deliberate divergences from V1

Both are strictly more conservative — each can only withhold a number V1 would
have published, never publish one V1 withheld.

1. **A device that returned no rows at all reads `None`, not `0.0`.** V1
   intersected an empty set with the window and published `0.0%`, making
   "no telemetry" indistinguishable from "dead all day".
2. **Plant aggregation returns `None` if any expected device is unknown**
   (the already-golden `weighted_sampled_availability`). V1's
   `calculate_weighted_plant_availability` dropped such devices and averaged
   the rest.

These are not theoretical. On the only day V1's contractual engine ever ran
(2026-07-24), **7 of 130 plants** had exactly one inverter with zero samples,
and V1 published a number anyway: asset 1883 was reported at **50.0%** when one
of its two inverters had simply returned no data. V2 reports `None` there.

### Monthly

Contractual months are **slot-weighted**, ported from V1's
`get_monthly_availability`:

```
SUM(availability_pct × valid_slots) / SUM(valid_slots)
```

not an arithmetic mean of days. Operational (sampled) months keep V1's own
different rule — a flat day mean gated on every day being complete. The two
are branched on `source_kind`, so a contractual source can never be aggregated
with the operational formula.

## 4. Real-data validation (V1 vs V2)

V1's contractual engine ran on exactly **one day, 2026-07-24** — 130 plants,
319 inverters, 54 593 raw power samples. Feeding V1's own stored
`inverter_power_samples` through V2's engine and comparing against V1's stored
results:

```
device-days compared: 319   exact: 312   divergent: 7
plant-days compared:  130   exact: 123   divergent: 7
plant valid_slots:    130   exact: 130   divergent: 0
```

Every one of the 7 divergences is divergence #1 above, verified individually:
each is a device with **zero** rows in `inverter_power_samples` that V1
published as `0.0%`. There is no algorithmic disagreement anywhere.

Separately, the live 2026-09-04 payload for asset 153 was run through both
implementations: V1 and V2 both produce **100.0%** and **47 valid slots**.

## 5. Scheduling

- `availability.history_sync` (off by default, one explicit connection,
  6-hourly): walks the trailing `lookback_days` of **closed** days
  oldest-first. A day whose `history_read` facts are already present is
  skipped *before* any HTTP call, so steady state costs zero provider calls.
  Only a genuinely new closed day, or one whose ingest previously failed,
  spends budget.
- A closed day's history is final, so old days are never re-fetched on a
  schedule. Moving one requires the explicit backfill script.
- Today is refused outright: a partial day would compute a lower availability
  than the day actually had. V1 refused the same way.

## 6. Corrections

History facts are keyed `fusionsolar-device-history:<device>:<instant>`, so a
provider correction for the same instant mints a **revision** superseding the
old row rather than overwriting it. The contractual calculation reads only
non-superseded rows — load-bearing, not tidiness: reading both would leave the
original value still voting, and a corrected 0 kW could never lower a number.

## 7. Limitations

- **Sigenergy remains structurally excluded** — no device-level contract
  exists, and none is proposed here.
- `inverter_state` is ingested but unused by the calculation (§3).
- Contractual availability has **not been enabled or backfilled in
  production**; the migration has not been applied to `nemsei_v2` either.
- The FusionSolar account is shared and was IP-blocked as recently as
  2026-08-24 (`FUSIONSOLAR_OWNERSHIP_WINDOW.md`). It is reachable again as of
  2026-09-06, but a fleet-wide backfill should be paced against the budget
  table in §2 rather than run in one burst.
