# FusionSolar canary validation (M4)

M4 proves the FusionSolar pipeline end to end on one installation before the
portfolio is switched on. This document records what was established without
calling the provider, what remains blocked, and the exact procedure to finish.

Status: the production pipeline is **validated end to end against real provider
payloads**; the **live network leg and the source-day boundary remain blocked**
for the reasons recorded below. Nothing was enabled on the live V2 runtime and
no provider request was made.

## Chosen canary

`Entre Vinhas e Mar`, FusionSolar plant `NE=157795675`.

| Property | Value |
| --- | --- |
| Installed DC power | 11.4 kWp |
| Devices | one inverter |
| V1 daily production | 2026-07-24 59.55 kWh, 07-23 63.53 kWh, 07-21 52.50 kWh |
| V1 selected field | `PVYield` on every recorded day |
| Specific yield | 4.6–5.6 kWh/kWp/day, physically consistent |

It was chosen for being small, single-inverter, operationally unimportant, and
for having recent V1 daily values whose magnitude is self-consistent with the
installed power, which makes it usable as a comparison baseline.

Two candidates were rejected. `Diaco - Casa` records 58.30 kWh on 5.4 kWp,
which is 10.8 kWh/kWp/day and physically impossible; its inverter is a
SUN2000-10KTL-M1, so the V1 `kwp` field is the value that looks wrong. That is
a V1 data-quality observation, not a V2 defect, and it disqualifies the site as
a measurement baseline. `Moviportas 3 - casa` has no recent daily values.

## Verified: PVYield is kWh

The V2 production adapter refuses to run without an operator-verified unit, and
the goal forbids assuming one. The evidence is V1's own recorded output across
the portfolio:

- V1 stored 54 896 FusionSolar production records and selected `PVYield` in
  52 720 of them; no other field was selected in a record that carries a value.
- Daily specific yield across the portfolio on 2026-07-24 falls between 4.25
  and 6.71 kWh/kWp, which is the expected range for July in Portugal. A Wh unit
  would put these values three orders of magnitude too low, and MWh three too
  high.

`PVYield` is therefore kWh, and `NEMSEI_V2_FUSIONSOLAR_<REF>_PRODUCTION_UNIT`
may be set to `kWh` for this account on evidence rather than assumption.

Note that V1 reads `PVYield`, `inverterYield` and `inverter_power` as a
fallback chain. V2 deliberately accepts only `PVYield` and treats an absent
value as missing rather than zero. That difference is intentional and must not
be reconciled towards V1.

## Not verified: the source-day timezone

`NEMSEI_V2_FUSIONSOLAR_<REF>_PRODUCTION_TIMEZONE` has no defensible value yet.

V1 builds the request boundary in
`monitoring_board/services/fusionsolar_models.py`:

```python
def collect_time_start_of_day_ms(collect_date: date) -> int:
    """Current behavior: local process timezone midnight for the requested day."""
    # TODO: Verify with FusionSolar whether collectTime must be portal timezone,
    # station timezone, or UTC. Existing app behavior uses local process timezone.
```

V1 uses its container's local timezone and states in code that the correct
semantics are unverified. Its `production_records.source_timezone` column is
`NULL` in all 54 896 rows, so nothing was recorded that could settle it. V1
therefore cannot answer this question, and V2 must not inherit an unverified
boundary into canonical facts.

Deciding it requires provider evidence. The procedure, once a window exists:

1. Pick a day whose production is well known for the canary.
2. Request `getKpiStationDay` for that day with `collectTime` at local midnight
   Europe/Lisbon, then at UTC midnight, then at portal-timezone midnight.
3. Compare the returned `PVYield` against the V1 value for that day and against
   the neighbouring days.
4. Whichever boundary reproduces the known day identifies the contract. If two
   hypotheses agree, repeat around a DST transition, where Europe/Lisbon and
   UTC diverge, so the ambiguity resolves.

Until that returns a single answer, the production leg stays blocked by V2's
own contract, which is the correct behaviour and is already covered by
`tests_v2/test_fusionsolar_production.py`.

## Blocked: account contention with production V1

V2 has no FusionSolar account of its own. The only credentials available are
V1's, and V1 is actively using that account right now.

- V1 ran successful FusionSolar syncs hourly on the day this was checked,
  including 10:32, 11:29 and 11:32 UTC.
- `docs/apis/fusionsolar.md` records that production, status and diagnostics
  share a persistent per-account lease specifically to prevent concurrent calls
  and bursts, with a daily KPI budget of 20 calls and a 65 second minimum
  interval.
- The same document records that a `failCode=407` applies a cooldown to the
  entire FusionSolar account and defers every FusionSolar job.

That lease lives inside V1 and V2 cannot join it. A second client on the same
account bypasses the coordination V1 was built around: V2's calls would consume
a shared budget invisibly, a V2 login may invalidate V1's cached session, and a
407 triggered by V2 would suspend monitoring for the whole portfolio. V1 is
frozen production for 267 installations, so this risk is not acceptable without
an explicit decision.

Three ways to unblock, in order of preference:

1. **A separate FusionSolar account for V2**, ideally read-only. This removes
   the contention permanently and is the only option that also scales to the
   portfolio later.
2. **An agreed window** with V1's FusionSolar jobs paused, accepting that V1
   monitoring is suspended for its duration. The canary needs roughly four
   requests: one login, one plant page, one `getStationRealKpi`, one
   `getKpiStationDay`. The timezone experiment adds two or three more.
3. **Accepting the risk** of running alongside V1. Not recommended: the failure
   mode is an account-wide cooldown affecting production monitoring, and its
   probability cannot be estimated from here.

## Validated: the production pipeline, on real provider data

V1 stored the raw `getKpiStationDay` responses it received. Replaying those
unmodified rows through V2's real client, service, request controller and
persistence — into a restored copy of the live V2 database, on the real canary
asset — exercises everything except the network call itself.

Result of the replay for 2026-07-23 and 2026-07-24:

| Evidence | Outcome |
| --- | --- |
| SyncRun | `success`, completeness `complete`, 3 provider calls, 2 items received, 2 accepted, 0 rejected |
| Request attempts | 3 `succeeded`, one per purpose: authentication, then one per source day |
| ProductionFact | 63.53 and 59.55 kWh, granularity `day`, quality `complete`, UTC period boundaries |
| SyncCursor | `last_completed_day=2026-07-24`, `covered_through=2026-07-25T00:00Z`, source timezone recorded |
| Idempotency | second identical run left exactly two facts |
| Against V1 | identical values for both days |

The canonical facts landed in PostgreSQL with the right value, unit, granularity,
quality and provenance, and the run left complete request evidence. The replay
ran on a restored copy rather than the live database so that no canonical fact
in production carries a provenance V2 did not obtain from the provider itself.

The captured row is pinned as a regression test in
`tests_v2/test_fusionsolar_real_payload.py`. It is worth keeping because the
provider disagrees with itself: on that day `PVYield` was 59.55 while
`inverterYield` was 58.9. V1 would fall back to the second field if the first
were absent; V2 accepts only `PVYield` and reports a missing value instead. A
synthetic fixture cannot demonstrate that, because only the provider puts
different numbers in those fields.

The same row also settles the unit independently: the provider's own
`perpower_ratio` of 5.224 equals 59.55 / 11.4, which only holds if `PVYield` is
kWh against a kW capacity.

## What could not be replayed

Current monitoring could not be validated this way, and this was established
exhaustively rather than assumed. Every V1 table carrying a payload or JSON
column was searched for `real_health_state`, the field that identifies a
`getStationRealKpi` response and that V2 maps to its verified plant health
codes. The result is zero rows, in `assets.source_payload`,
`device_realtime_snapshots.payload_json`, `provider_devices.payload_json`,
`integration_sync_runs.summary_json`, `production_records.payload_json` and
`availability_daily.payload_json` alike. `monitoring_records` has no payload
column at all, `integration_realtime_snapshots` holds Sigenergy rows only, and
`assets.source_payload` is an Excel import row rather than provider data.

V1 therefore never persisted a station status response. There is no genuine
payload to replay, and fabricating one would prove nothing about the provider
contract while looking like evidence. Current monitoring needs a live call.

Authentication is in the same position. The replay exercises V2's login path,
header token extraction and session reuse, but against a replayed response; only
a real account can prove the credential exchange itself.

## What is ready

Everything that does not require the provider is in place. The V2 slice already
implements authentication, guarded discovery, current monitoring and daily
production history behind capability gates, with sync runs, request attempts,
integration health, canonical observations, current-state projection and
production facts. The refusal paths for a missing timezone, a missing unit and
disabled reads are covered by tests and need no new work.

To run the canary once unblocked:

1. Place the account credentials in
   `/opt/server/apps/Nem-sei-v2-data/secrets/` as
   `fusionsolar_<ref>_username` and `fusionsolar_<ref>_password`, mode 600, and
   mount them as Docker secrets.
2. Set `NEMSEI_V2_FUSIONSOLAR_<REF>_BASE_URL`, `_USERNAME_FILE`,
   `_PASSWORD_FILE` and `_PRODUCTION_UNIT=kWh` in `.env.v2`, remembering that
   every literal `$` must be escaped as `$$`.
3. Create the provider connection with that credential reference, configure and
   enable it, and activate a source policy for the canary mapping only.
4. Enable `NEMSEI_V2_PROVIDER_READS=true`. Leave
   `NEMSEI_V2_PROVIDER_MUTATIONS`, notifications and report distribution false.
5. Validate the connection, then run current monitoring, then resolve the
   timezone experiment, and only then enable daily production.
6. Compare every result against the V1 baseline for the canary before declaring
   the pipeline sound, and disable reads again until the portfolio decision.
