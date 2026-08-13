# Nem-sei V2 migration matrix

No V1 data migration is implemented in the foundation. Future import tools
open V1 SQLite read-only without importing V1 Python. Each domain migration
must define source tables, target ownership, natural keys, count/total checks,
quality-state reconciliation, and sampled-record validation.
