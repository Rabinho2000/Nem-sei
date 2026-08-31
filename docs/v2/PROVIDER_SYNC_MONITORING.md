# Provider, sync, and monitoring foundation

This milestone adds only provider-neutral contracts and persisted control/data
models. FusionSolar has narrow, guarded discovery, current-monitoring, and
daily production-history slices. Sigenergy has narrow, guarded connection
validation, system discovery, and current energy-flow monitoring. SMA remains
a registry descriptor with every operational capability unsupported. Provider
response structures are confined to their adapter packages and never enter
canonical-domain code.

Capability implementation support (`supported` or `unsupported`) is distinct
from runtime availability (`available`, `not_configured`,
`temporarily_unavailable`, or `unknown`). A configured connection with no
implemented adapter is therefore `unsupported` plus runtime `unknown`; it is
never represented as a plant outage.

`integration_health` records auth/access, provider availability, discovery,
quota, sync, partial, stale, and safe attempt/error timestamps per connection.
It does not alter `Asset` state or write offline observations. Providers errors
are normalized to a small safe taxonomy, including authentication,
authorization, unavailable, rate-limited, timeout, transport, invalid response,
not-supported, and unknown.

`sync_runs`, `sync_cursors`, `provider_request_states`, and
`provider_request_attempts` make deferred work and actual provider-call
evidence durable. Unknown quotas stay unknown: scheduling does not invent a
numeric budget. A `Retry-After` becomes a persisted deferral. Only a successful
sync may advance a cursor/coverage checkpoint; partial, failed, deferred, and
rate-limited runs cannot advance it.

`monitoring_observations` and `production_facts` are append-only PostgreSQL
records. Monitoring condition and freshness are independent. Production starts
with `production_energy`; a missing value is explicitly `missing` and is not
zero. Idempotent replay returns the existing fact; corrections create a new
source revision that points to the superseded record.

`asset_source_policies` selects canonical monitoring or production sources by
asset, mapping, priority, validity interval, and fallback intent. Equal primary
priorities are reconciliation conflicts, never automatic double counting.
Legacy mapping priority columns are retained only for migration compatibility;
the migration seeds temporal policies where those values already exist.

## FusionSolar daily production history

The only production endpoint currently implemented is the frozen-V1-evidenced
daily KPI request: `POST /thirdData/getKpiStationDay` with up to 100
`stationCodes` and a `collectTime` at the start of one configured source day.
There is no verified range or pagination contract, so a sync authenticates once
and issues one guarded daily request per selected-mapping batch and source day.
It never performs discovery.

V1 evidence does not establish which timezone defines the provider day or the
unit of `PVYield`. Before this capability can make a network request, the
connection's credential-reference environment must set both:

```text
NEMSEI_V2_FUSIONSOLAR_<REFERENCE>_PRODUCTION_TIMEZONE=<verified IANA timezone>
NEMSEI_V2_FUSIONSOLAR_<REFERENCE>_PRODUCTION_UNIT=kWh
```

Only `PVYield` is accepted as the daily `production_energy` signal. Other V1
fallback fields are not canonicalized. A present numeric zero is a complete
zero fact; absent/null `PVYield` is a persisted missing/partial fact; a missing
plant row is partial coverage and does not create a synthetic zero.

`ProductionFact` stores UTC boundaries plus the source period date, configured
source timezone, and source field. The cursor stores the last fully completed
source day and only advances if a successful window extends existing coverage
contiguously. A successful gap window may preserve facts but cannot imply
coverage for skipped days. Partial, failed, deferred, rate-limited, and
historical-correction runs leave it unchanged. A stored cursor timezone must
match the currently verified connection timezone or the run stops as a
configuration/data-contract finding. Normal syncs have a configurable
application safety cap (default 31 source days); larger history loading needs a
future explicit bounded backfill path. A later caller may request a small
overlap (including D-1) for correction reconciliation; unchanged facts are
idempotent and changed facts create immutable revisions.

## Sigenergy read-only slice

The verified V1 evidence and deterministic adapter fixtures establish the
Sigenergy AppKey/AppSecret login envelope, bearer token use, `sigen-region`
header, a read-only system-list request, and a read-only
`/openapi/systems/{system_id}/energyFlow` request. V2 keeps these calls behind
the durable request controller and a non-secret credential reference. System
identifiers use the existing connection-scoped provider rule; discovery only
reports mapped, unmapped, or conflict candidates and never creates assets or
mappings.

Current monitoring reads only source-policy-selected mappings and does not run
discovery. The V1-evidenced `normal`, `online`, `running`, `fault`, `error`,
`abnormal`, `offline`, and `disconnected` words are normalized conservatively;
an unrecognized status remains `unknown`. The endpoint has no verified
provider observation timestamp, so freshness remains `unknown` and receipt
time is recorded as ingestion evidence.

An **absent** status is a different case, and since 2026-08-25 it is no longer
simply `unknown`. `energyFlow` carries no status field at all: V1's own stored
record of a live response for this account
(`provider_system_inventory.metadata_json`, system `TZXRS1780315946`) lists
exactly `acPower`, `batteryPower`, `batterySoc`, `evPower`, `gridPower`,
`heatPumpPower`, `loadPower`, `pvPower` — and V1's inventory row for that same
system still reads `operational_status = 'unknown'` today. Reading only for a
status field therefore condemned every Sigenergy plant to "desconhecido"
forever, however healthy it was. So when no status field is present, the
condition is read from the flow: a positive `pvPower`/`acPower` is
`operational`, because generating power is not a hint about what the plant
might be doing, it is the plant doing it. A stated status always wins over the
flow, including a stated fault while power is still flowing. A complete payload
with nothing generating stays `unknown` but is recorded as a *complete* read,
so that every night on the account does not report as a degraded sync; only an
energyFlow with no fields at all is still `partial`. `condition_source` in the
observation metadata records which of these produced the answer. Each mapping is reserved and marked as
attempted immediately before its provider call. A failed mapping does not abort
the remaining mappings; a rate-limit deferral may stop the remainder and marks
those mappings skipped. Sync metadata records `expected_items`,
`attempted_items`, `items_received`, `items_accepted`, `items_rejected`,
`items_failed`, `items_skipped`, and `actual_provider_calls`.

Sigenergy discovery establishes identity only from the verified `systemId` and
`systemName` fields. Alternate fields such as `id`, `stationId`, or `plantId`
are not silently promoted to canonical identity. Provider/API failure updates
SyncRun, request-attempt, and IntegrationHealth records but never writes an
offline observation. Authentication, authorization, rate limiting, invalid
responses, and transport failures affect their corresponding health dimensions;
none of them means that a plant is offline.

Date-only source policies are resolved in each asset's explicit IANA timezone
from the current UTC instant. An asset with no valid timezone is a source-policy
finding and is not queried; the provider adapter does not assume Lisbon or
another global timezone.

Sigenergy daily production is deliberately not implemented. Although legacy
fixtures contain kWh-named fields, source-day/timezone semantics, correction
behavior, and safe historical request bounds are not independently verified.

## Plant-state scheduling

Current monitoring is scheduled from 2026-08-25 as the job type
`monitoring.current`, one schedule per provider account
(`monitoring.current:<connection_id>`), dispatched to the right service by the
connection's `provider_code`. Before that it had no job type at all: the
services existed and were tested, and nothing ever called them, so the
plant-state half of the product was dark while the device half ran.

Cadence is 15 minutes and the interval is in minutes rather than hours because
this is the read outage alerting depends on -- a plant that fell an hour ago is
news that arrived an hour late. It is affordable at that cadence because
FusionSolar answers up to 100 plants per batched call: the whole mapped fleet
of 134 costs two calls plus a cached login, measured, not estimated. Sigenergy
has no batch endpoint, so it costs one call per selected mapping.

Rate-limit budgets are tracked per `endpoint_family` in
`provider_request_states`, and `current_monitoring` is its own family -- the
pressure visible on this account today sits on `device_discovery` and
`device_current_monitoring`, which this does not share.

Neither service writes an observation when a call fails, so a rate-limited or
failed read can never present as an offline plant. The job also reports success
when the provider refuses: the refusal is recorded on the sync run and the
connection health, and failing the job would only retry it against an account
that just said no.

## Production recovery modes

`incremental` remains cursor-driven and capped at 31 days by default.
`reconciliation` is an explicit provider-local D-1 refresh (or a separately
bounded small recent window): it never moves the incremental cursor. Its
missing/partial evidence never replaces an existing complete fact, while a
changed complete value produces an immutable revision. `bounded_backfill`
requires both dates, has a separate bounded overall window and chronological
chunk cap, and records the next source day in its persisted job payload before
the worker releases its lease. Rate limits and failures retain that progress for
retry; there is no autonomous backfill scheduler.

Canonical `production_coverage` is provider-neutral and makes no network call.
It classifies each explicit source-timezone day from latest `ProductionFact`
evidence as `complete`, `partial`, or `missing`; a numeric complete zero is
complete. Backfill may extend the cursor only through a complete contiguous
window, never through a policy conflict, missing day, or partial day.

Latest provider evidence and an effective complete numeric value are separate
canonical queries. For example, a complete `120 kWh` revision followed by a
missing/partial revision retains both immutable records: coverage/cursor safety
uses the latest missing evidence, while a future quality-aware consumer can
retrieve the prior complete `120 kWh` fact explicitly. A later complete
correction becomes both latest evidence and effective complete value. No raw
provider payload or secret is retained.
