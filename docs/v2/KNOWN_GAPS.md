# Nem-sei V2 known gaps

V2 includes the foundation plus assets, a canonical device level,
provider-neutral contracts, sync control, integration health, canonical
monitoring/current-state persistence, temporal source policy, and narrow guarded
FusionSolar discovery, current-monitoring, and daily production-history reads.
It also holds persisted financial models, reporting datasets and snapshots, the
ported reporting rules, both renderers, and the assembly layer between them. It
intentionally excludes realtime production/performance calculations and
automation/distribution. Portfolios now exist with temporal membership, dynamic
rules, frozen snapshots, an aggregate dataset built from the per-asset reports,
a monthly gerar/rever/aprovar workflow, and a dedicated screen for reviewing
and resolving members with no installation yet. Reporting has a real web
surface at `/reports` — individual and portfolio reports, generated, listed,
and downloaded as PDF or Excel from the browser — where previously generating
one meant calling a Python function directly.

A complete asset report can be produced from V2's database alone.
`production_facts` carries five energy metrics, tariffs and billing
configuration are persisted with temporal validity, and `Asset` holds the
contract attributes that resolve EPC against ESCO. What remains without any
source is named in every payload's `unavailable_fields`: the four self-use
splits by tariff period, which no provider V2 talks to states, and
`availability_pct`. That one is not a missing import: V1's own weighted
availability calculation, run against V1's own history, produces a real value
for only 3 of 5 121 plant-days (0.06%) because `device_realtime_snapshots`
samples most device-days only 1-6 times — nowhere near dense enough for its
90-minute-gap completeness rule. Porting the algorithm would not change that;
the gap is sampling density, not code. See `DIAGNOSTICS.md`.

Two limits are worth stating plainly. The additional energy metrics enter V2 by
importing payloads V1 already stored; the **live** FusionSolar contract is still
verified for `PVYield` only, so a plant with no V1 history has production and
nothing else until those signals are verified per connection. And a tariff
prices energy without stating a euro total, because V1 computes that from hourly
rows split by tariff period and V2 holds no hourly facts.

Devices carry canonical identity and, now, historical status. Just the
`inverter` kind is populated, because every V1 device is an inverter; meters,
dataloggers and string boxes are vocabulary with no rows. `device_status_facts`
holds point-in-time availability, active power and day energy imported from
V1's `device_realtime_snapshots`, keyed on device identity rather than a
provider mapping, because none of the 325 device claims M1 imported is
`active`.

A **live** FusionSolar device-level read now exists (M7 Fatia 2, see
`docs/v2/DEVICE_TELEMETRY.md`) and has been **canary-proven against the real
account**, 2026-08-20: `getDevList` + `getDevRealKpi`, both V1-evidenced,
behind their own verified-contract gate (`FusionSolarDeviceContract`,
requiring an explicit operator-verified active-power/day-energy unit, since
V1's own unit handling was an unverified magnitude guess, not a contract).
`device_status_facts` gained `freshness`/`quality`/`completeness`/`sync_run_id`
(migration `0016`, applied to the live database with a verified backup) to
carry this evidence with the same provenance `monitoring_observations` already
has. The live canary (asset 153, two inverters, real production account,
`docker pause`d V1 for 5.63 s total across two windows) confirmed inverter
state, active power (`kW`), and day energy (`kWh`) — and confirmed
`collectTime` is genuinely absent from this endpoint's response, not just
unchecked. It caught and fixed one real defect (a device-identity field
priority bug) before any wrong fact was persisted. M7 Fatia 3 then added a
persistent, restart-safe, idempotent poll schedule (reusing the existing
`Job`/`ScheduleState` infrastructure, no new tables) and ran it live for a
real 70-minute/30-minute-cadence window on the same one asset: 3/3 cycles
succeeded, 6/6 readings, 0 failures, largest gap 30 min 03 s, 9 of a 15-call
hard cap. That density is strongly extrapolated, not yet provably measured
for a full operating day, as sufficient for `sampled_availability.py`'s
90-minute-gap rule — availability stays off until a full-day run is replayed
through its actual completeness rules, not extrapolated. See
`DEVICE_TELEMETRY.md` §5-§8. Sigenergy has
**no** device-level contract to build on at all: V1 never called a Sigenergy
device endpoint (0 of 325 `provider_devices` rows, 0 of 51 289
`device_realtime_snapshots` rows), and V1's own docs list
inverters/strings/availability as explicitly out of scope — this is not a
missing import, there is nothing to import from.

A later session (2026-08-20) recovered from a suspected connection loss
during that 70-minute window and found the window had, in fact, completed
cleanly on its own schedule (`DEVICE_TELEMETRY.md` §9.1). The real gap it
found instead: the three live cycles were driven by a session-local
one-off `Scheduler`/`Worker` pair, not the standing `nemsei-v2-scheduler-1`
container, which never had polling enabled — the mechanism was correct, its
deployment was not persistent (§9.2). Fixed: `Settings` now requires an
explicit, positive lifetime cycle cap (`device_status_poll_max_cycles`)
whenever polling is enabled, enforced by counting real jobs back from the
`jobs` table itself, not a separate counter (§9.3) — closing the "does this
survive without an interactive session" question for good. A day-plus
canary (48 lifetime cycles, enough to cross a full daylight operating
window per a fresh re-read of `sampled_availability.py`'s actual
operating-window definition — see `DIAGNOSTICS.md`'s 2026-08-20 addendum)
is fully prepared (`docker-compose.v2.device-status-canary.yml`, a verified
backup taken) but **BLOCKED**, deliberately not forced through: the one
live-database write it needs was refused by this session's own action
classifier as a production write, correctly, for something this
consequential — see `DEVICE_TELEMETRY.md` §10 for the exact commands
awaiting a human's go-ahead.

A first deterministic operational-findings layer now also exists (M7
Fatia 4, `diagnostics/findings.py`): device-unavailable, unknown-status,
no-history, stale-reading, zero-power-while-peers-active, power/day-energy
disparity among comparable devices at the same asset, and partial device
coverage, each recomputed fresh from `device_status_facts` on every page
load rather than persisted with an open/acknowledged/resolved lifecycle —
deliberately, since nothing operational yet needs that lifecycle and a
recomputed-not-stored finding cannot itself become stale or duplicate.
Wired into `/diagnostics/assets/<id>`, worst-first, alongside the existing
per-device table. Not yet an asset-level severity summary or a portfolio
view — see `ROADMAP.md`.

FusionSolar daily production remains gated on an operator-verified source
timezone and `PVYield=kWh` contract per connection. Sigenergy has guarded
read-only connection validation, discovery, and current monitoring; its daily
production remains blocked until source-day/timezone and bounded-history
semantics are independently verified. SMA has no live adapter. No live
provider call is made by tests or by default policy.
It has no persisted users or RBAC; the administrator is configured with a
password hash.
