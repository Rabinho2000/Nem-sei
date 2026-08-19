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

## Reconciliation of the import residue

The identity import left 21 conflicts, 2 quarantines, 5 exclusions and 56
unresolved integrations. Each group was investigated against the frozen V1
snapshot; most were closed evidence rather than open work.

**Duplicate installations (2 quarantined, 5 excluded).** V1 assets 2078 and 2102
share the name "Lopal" and every field. 2078 carries three aliases, the
FusionSolar plant mapping, one inverter, 95 monitoring records, 177 production
records and 5 alerts; 2102 carries nothing. Quarantining both had also excluded
all of 2078's children, so V2 was missing a real 23.6 kWp installation. An
operator decision designated 2078 canonical and discarded 2102, and the
importer then created the asset, its 3 aliases, its plant mapping and its
inverter. The decision mechanism is described below.

**Alias conflicts (21).** These are not duplicated rows. Each is the same
normalized alias with different raw text, such as
`DIALOGOS DO BOSQUE UNIPESSOAL LDA` from `mapping_confirmed` beside
`Diálogos do Bosque, Unipessoal, LDA` from `excel`. Lookup is unaffected
because the normalized alias is identical; only the displayed text differs, and
V2 keeps whichever V1 row came first. No action was taken: choosing a different
raw variant would churn identity data for a cosmetic gain.

**Unresolved integrations (56).** Cross-checking every unresolved external ID
against V1's own `asset_integrations` shows 50 were already resolved by
creating a real mapping, which V2 imported. They are closed historical
evidence. Six records remain genuinely unmapped and two of those describe the
same plant, leaving five distinct FusionSolar plants that were never associated
with any V1 asset: `NE=141213422`, `NE=155139007`, `NE=271939998`,
`NE=310640320` and `NE=319590186`. Each needs an operator ruling on whether it
is a missing installation, someone else's plant visible in the account, or a
test entry. They stay recorded as `unresolved`; nothing is auto-mapped.

## Operator identity decisions

`legacy_identity_decisions` records one ruling per ambiguous V1 source row:
`canonical` for the row that represents the real installation, `discard` for
the rows that do not. A duplicate group is only resolved when exactly one
member is canonical and every other member is explicitly discarded; anything
less stays quarantined.

```text
python -m nemsei.assets.identity_decisions \
  --legacy-table assets --legacy-id 2078 --decision canonical \
  --actor <operator> --reason "<evidence>"
```

Each ruling writes an operator audit event. Re-running the importer replays the
decision, so the resolution is reproducible from the snapshot rather than being
a manual database edit. A decision reopens a row that a previous run
quarantined; rows that already produced V2 data are never reopened, and the
earlier quarantine stays on the record beside the new outcome.
