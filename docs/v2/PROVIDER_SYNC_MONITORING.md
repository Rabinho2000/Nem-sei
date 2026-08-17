# Provider, sync, and monitoring foundation

This milestone adds only provider-neutral contracts and persisted control/data
models. FusionSolar has narrow, guarded discovery, current-monitoring, and
daily production-history slices. Sigenergy and SMA remain registry descriptors
with every operational capability unsupported. Provider response structures are
confined to the FusionSolar adapter and never enter canonical-domain code.

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
source day. It advances only after all requested days have been persisted with
complete selected-mapping coverage; partial, failed, deferred, and rate-limited
runs leave it unchanged. A later caller may request a small overlap (including
D-1) for correction reconciliation; unchanged facts are idempotent and changed
facts create immutable revisions.
