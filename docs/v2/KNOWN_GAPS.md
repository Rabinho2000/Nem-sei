# Nem-sei V2 known gaps

V2 includes the foundation plus assets, a canonical device level,
provider-neutral contracts, sync control, integration health, canonical
monitoring/current-state persistence, temporal source policy, and narrow guarded
FusionSolar discovery, current-monitoring, and daily production-history reads. It
intentionally excludes portfolio membership, realtime production/performance
calculations, financial models, reports, and automation/distribution.

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
