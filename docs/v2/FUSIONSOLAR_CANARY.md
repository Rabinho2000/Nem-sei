# FusionSolar canary validation (M4)

M4 proves the FusionSolar pipeline end to end on one installation before the
portfolio is switched on.

Status: **validated on the canary**, after the live run exposed and fixed a
day-attribution defect. Executed 2026-08-19 in three short windows with V1's
FusionSolar activity paused, totalling 3 minutes 24 seconds of V1 downtime and
8 provider requests.

## Canary

`Entre Vinhas e Mar`, asset 180, FusionSolar plant `NE=157795675`, 11.4 kWp,
one inverter. Chosen for being small, single-inverter, operationally
unimportant, and for having V1 daily values whose magnitude is self-consistent
with the installed power.

Only this installation was activated: one enabled connection, one active
mapping, two source policies. Every other mapping stayed `pending_review` on the
disabled legacy connections.

## What the live run established

**Authentication.** `/thirdData/login` succeeded against
`https://eu5.fusionsolar.huawei.com` with the account's mounted credentials.

**Current monitoring.** One `getStationRealKpi` call for one plant produced a
canonical `MonitoringObservation`: condition `operational` derived from the
provider's raw health code `3`, quality and completeness `complete`, and
freshness `unknown` with metadata `observed_at_source =
ingested_at_no_provider_timestamp`. The response carries no provider timestamp
and V2 did not invent one. `MonitoringCurrentState` recorded the confirmation
against the successful sync run.

**Daily production.** One `getKpiStationDay` call produced a `ProductionFact`
of **59.56 kWh** for 2026-07-24, against **59.55 kWh** independently recorded by
V1 for the same day. The 0.01 difference is provider rounding or a later
revision; the agreement confirms the whole chain.

Every leg recorded its `SyncRun`, per-request `ProviderRequestAttempt` rows and
`IntegrationHealth`, with `actual_provider_calls` matching the requests made.

## Defect found and fixed: day attribution

`getKpiStationDay` does not answer with the requested day. It answers with **one
row per day of the month**, all carrying the same `stationCode` and differing
only by `collectTime`. The live request for a single station and a single day
returned 31 rows.

The adapter matched rows by station code alone and accepted the first one, so
the fact for 2026-07-24 was filed with **69.72 kWh — the value for 2026-07-01**.
Silently attributing one day's energy to another date is the worst possible
failure for reporting, and no fixture caught it because V1 had stored a single
row per record.

The fix is provider-neutral: a row now carries the instant it describes, taken
from its own `collectTime`, and is accepted only when that instant falls inside
the requested source day in the connection's contracted timezone. Rows for other
days of the month are counted as out-of-window rather than rejected, so a normal
response no longer marks a run partial. A row without a usable timestamp cannot
be attributed at all and is refused rather than guessed.
`tests_v2/test_fusionsolar_month_response.py` pins the behaviour against a
realistic 31-row response.

The corrected run superseded the wrong fact rather than deleting it: the
database refuses deletion with `canonical facts are append-only`, so revision 1
(69.72) remains on record superseded by revision 2 (59.56). The correction is
fully auditable.

## Verified: the source-day contract

The question left open by V1 is now answered from the provider's own response.
Every one of the 31 rows carries a `collectTime` that is an exact **UTC
midnight**:

```
1782864000000 → 2026-07-01T00:00:00+00:00   PVYield 69.72
1784851200000 → 2026-07-24T00:00:00+00:00   PVYield 59.56
1785456000000 → 2026-07-31T00:00:00+00:00   PVYield 64.16
```

The account labels its daily buckets by UTC midnight, so
`NEMSEI_V2_FUSIONSOLAR_PRIMARY_PRODUCTION_TIMEZONE=UTC` selects the row the
provider itself labels as that calendar day, and the resulting value matches
V1's independent record. The cursor stores the timezone alongside the day, so a
later change stops incremental continuation until an operator reconciles it.

One honest limit: this verifies the **labelling** contract, not the physical
integration window. Whether the provider accumulates energy over a UTC day or a
plant-local day is not separable from this evidence. For these Portuguese
installations the distinction has no practical effect at day granularity,
because the hour that shifts between the two carries no production. It would
matter for sub-daily facts, which V2 does not collect.

## Verified: PVYield is kWh

Established before the live run, from V1's own output: `PVYield` was the field
selected in 52 720 of 54 896 records, and portfolio daily specific yields on
2026-07-24 fall between 4.25 and 6.71 kWh/kWp, the expected range for July in
Portugal. The live canary value of 59.56 kWh on 11.4 kWp is 5.22 kWh/kWp, inside
that range. V1 reads `PVYield`, `inverterYield` and `inverter_power` as a
fallback chain; V2 accepts only `PVYield` and treats an absent value as missing
rather than zero. That difference is intentional.

## Still blocking the portfolio

**The account is shared with production V1.** V2 has no FusionSolar account of
its own. V1 coordinates its production, status and diagnostics jobs through a
persistent per-account lease with a daily KPI budget, and a `failCode=407` puts
the whole account into cooldown and defers every V1 job. A second client cannot
join that lease. The canary was only possible because V1 was paused; scaling to
135 plants alongside a running V1 is not.

A separate FusionSolar account for V2, ideally read-only, removes the contention
permanently and is the prerequisite for any portfolio rollout.

**Two operational notes from the canary.** The asset had no timezone of its own,
so V2 correctly refused to resolve a source policy and made zero provider calls;
it was set to `Europe/Lisbon` from the installation's postal address in Óbidos.
Any other installation will need the same before it can be activated. And V1's
own July backfill for this plant wrote the identical value 52.5 to eight
different days, which is the same class of defect V2 just fixed — V1's
backfilled daily history should not be trusted as a reporting baseline.
