# Nem-sei V2 known gaps

V2 includes the foundation plus assets, provider-neutral contracts, sync control,
integration health, canonical monitoring/current-state persistence, temporal
source policy, and narrow guarded FusionSolar discovery, current-monitoring, and
daily production-history reads. It intentionally excludes portfolio membership,
realtime production/performance calculations, financial models, reports, and
automation/distribution.

FusionSolar daily production remains gated on an operator-verified source
timezone and `PVYield=kWh` contract per connection. Sigenergy and SMA have no
live adapter. No live provider call is made by tests or by default policy.
It has no persisted users or RBAC; the administrator is configured with a
password hash.
