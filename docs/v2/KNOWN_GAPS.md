# Nem-sei V2 known gaps

V2 includes the foundation plus assets, a canonical device level,
provider-neutral contracts, sync control, integration health, canonical
monitoring/current-state persistence, temporal source policy, and narrow guarded
FusionSolar discovery, current-monitoring, and daily production-history reads.
It also holds persisted financial models, reporting datasets and snapshots, the
ported reporting rules, both renderers, and the assembly layer between them. It
intentionally excludes portfolio membership, realtime production/performance
calculations, and automation/distribution.

A report can be produced from V2's database alone, but not yet one a customer
could receive. `production_facts` carries a single metric, `production_energy`,
so self-consumption, export, consumption and grid import have no source. There
are no tariff or billing-configuration tables, and `Asset` carries no commercial
attributes, so report type falls back to EPC rather than being resolved. Every
such field is emitted as `None` and named in the payload's `unavailable_fields`
rather than rendered as a zero. `REPORTING_PARITY.md` lists all twenty-one.

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
