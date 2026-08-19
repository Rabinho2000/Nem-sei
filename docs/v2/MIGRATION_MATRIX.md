# Nem-sei V2 migration matrix

V1 remains the source of truth until an explicit cutover. Migration is
deliberate and per domain: no table is copied merely because it exists. Import
tools open V1 SQLite read-only and never import V1 Python.

Every domain migration must define its source tables, target ownership, natural
keys, count and total checks, quality-state reconciliation, and sampled-record
validation before its data is accepted.

## Migrated

| Domain | V1 source | V2 target | Natural key | Validation |
| --- | --- | --- | --- | --- |
| Owners | `customers` | `organizations` | Normalized tax ID | Row counts, per-row source hash, duplicate tax IDs reviewed |
| Installations | `assets` | `assets` | Normalized name per source database | Row counts, per-row source hash, duplicate names quarantined |
| Aliases | `asset_aliases` | `asset_aliases` | Asset plus normalized alias | Parent asset must exist, duplicates recorded as conflicts |
| Plant claims | `asset_integrations` | `asset_provider_mappings` (`resource_kind='plant'`) | Connection plus normalized external ID | Connection-scoped collision check, imported as `pending_review` |
| Unresolved integrations | `integration_unresolved` | `legacy_import_records` evidence only | V1 row ID | Recorded as `unresolved`; never auto-mapped |
| Devices | `provider_devices` | `devices` plus `asset_provider_mappings` (`resource_kind='device'`) | Serial per asset; connection plus normalized external device ID | Row counts, per-row source hash, unknown device types quarantined, orphan devices excluded |

Device import details, including the column-by-column mapping and what is
deliberately left behind, are in `DEVICE_MODEL.md`.

## Not migrated

| V1 source | Reason |
| --- | --- |
| `provider_device_configuration_history` | All 325 rows are single-version and open; there is no device history to carry over |
| `provider_device_expected_strings` | String-level expectation belongs with performance work, not identity |
| Monitoring, production, availability and inverter sample tables | Facts are regenerated from providers, not migrated |
| Contracts, portfolios, financial models, reports, tariffs, invoices | Their V2 domains do not exist yet; each needs its own matrix row first |
| Credentials, endpoints, payloads, provider snapshots | Never imported |
| `background_jobs` | V1-specific states; V2 has its own queue contract |

## Outstanding reconciliation

The identity import left evidence that still needs a human decision: 21
conflicts, 2 quarantines and 56 unresolved integrations. They are recorded in
`legacy_import_records` with their reasons and are tracked as milestone M3 in
`ROADMAP.md`.
