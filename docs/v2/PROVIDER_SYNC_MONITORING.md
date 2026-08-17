# Provider, sync, and monitoring foundation

This milestone adds only provider-neutral contracts and persisted control/data
models. FusionSolar, Sigenergy, and SMA remain registry descriptors with every
operational capability unsupported. There are no HTTP clients, credentials,
provider API calls, or provider-specific response structures in V2.

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
