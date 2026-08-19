# Nem-sei V2 known gaps

V2 includes the foundation plus assets, a canonical device level,
provider-neutral contracts, sync control, integration health, canonical
monitoring/current-state persistence, temporal source policy, and narrow guarded
FusionSolar discovery, current-monitoring, and daily production-history reads.
It also holds persisted financial models, reporting datasets and snapshots, the
ported reporting rules, both renderers, and the assembly layer between them. It
intentionally excludes portfolio membership, realtime production/performance
calculations, and automation/distribution.

A complete asset report can be produced from V2's database alone.
`production_facts` carries five energy metrics, tariffs and billing
configuration are persisted with temporal validity, and `Asset` holds the
contract attributes that resolve EPC against ESCO. What remains without any
source is named in every payload's `unavailable_fields`: the four self-use
splits by tariff period, which no provider V2 talks to states, and
`availability_pct`, which needs device-level facts.

Two limits are worth stating plainly. The additional energy metrics enter V2 by
importing payloads V1 already stored; the **live** FusionSolar contract is still
verified for `PVYield` only, so a plant with no V1 history has production and
nothing else until those signals are verified per connection. And a tariff
prices energy without stating a euro total, because V1 computes that from hourly
rows split by tariff period and V2 holds no hourly facts.

Devices exist as canonical identity only. Just the `inverter` kind is populated,
because every V1 device is an inverter; meters, dataloggers and string boxes are
vocabulary with no rows. No device-level fact is collected yet, no provider
device discovery is wired to the device level, and device claims imported from
V1 stay `pending_review` on disabled, credential-free legacy connections.

FusionSolar daily production remains gated on an operator-verified source
timezone and `PVYield=kWh` contract per connection. Sigenergy has guarded
read-only connection validation, discovery, and current monitoring; its daily
production remains blocked until source-day/timezone and bounded-history
semantics are independently verified. SMA has no live adapter. No live
provider call is made by tests or by default policy.
It has no persisted users or RBAC; the administrator is configured with a
password hash.
