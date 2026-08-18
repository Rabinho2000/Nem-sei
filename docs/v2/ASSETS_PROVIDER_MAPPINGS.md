# Assets and provider mappings

This milestone defines the V2 physical-installation boundary and the
operator-controlled review layer. Provider calls remain behind the existing
capability gates; this layer adds no provider-specific schema or automatic
activation.

## Identity rules

- `organizations` is a small owner boundary, not a CRM or billing model.
- `assets` represents one physical installation. Its immutable `public_id` is
  suitable for URLs and external references; its integer ID is internal.
- Names and aliases are normalized for lookup, but are never globally unique.
  Ambiguity is a review condition, never a name-based automatic merge.
- An imported asset with no trustworthy timezone receives `Europe/Lisbon` with
  `timezone_source=migration_default`; it is explicitly reviewable rather than
  a provider-confirmed value.
- Lifecycle is deliberately separate from V1 O&M contract state.

## Provider mapping rules

The code-owned registry currently knows `fusionsolar`, `sigenergy`, and `sma`.
It is declarative and network-free. FusionSolar supports its guarded read
slice; Sigenergy supports only its guarded connection-validation, discovery,
and current-monitoring read slice; SMA has no supported capability yet.

`provider_connections` contains only a non-secret credential reference name,
not a password, token, endpoint, or payload. External plant identifiers are
normalized and unique only within a connection. A single connection can have
one active claim for a normalized plant ID. Replacements supersede and close
the previous mapping instead of overwriting it. Connection scope is intentional:
cross-account collisions are reconciliation conflicts until an adapter contract
proves provider-global identifier semantics.

## V1 import

Run an explicit migration first, then use the importer from the V2 environment:

```text
python -m nemsei.assets.v1_import --v1-db /path/to/monitoring_board.db --dry-run
python -m nemsei.assets.v1_import --v1-db /path/to/monitoring_board.db
```

The source is opened with SQLite URI `mode=ro` and `PRAGMA query_only=ON`.
The importer uses SQL only; it cannot import `monitoring_board` and cannot
write V1. It reads only V1 customers, assets, aliases, and asset integrations.
It excludes credentials, endpoints, payloads, contracts, portfolio rows,
monitoring data, and provider snapshots.

Each real run stores a source checksum, source-locator hash, importer version,
and per-row hash in `legacy_import_runs`
and `legacy_import_records`. A dry run writes no V2 rows and prints a JSON
manifest. Replays reuse the recorded target. If V1 source evidence changes, the
importer creates a review/audit outcome and preserves the V2 record, including
manual edits. Real imports commit deterministic batches of at most 100 audited
writes, so an interrupted run can safely be rerun. Duplicate normalized V1 asset names are quarantined rather than
created or merged. Legacy FusionSolar and Sigenergy mappings are connected only
to disabled, credential-free V2 legacy connections.

## Operating constraints

There remains one worker and short SQLite units of work. Network work, parsing,
and rendering stay outside transactions. The later adapter milestone must add
request budgets, sync evidence, canonical facts, and per-handler idempotency
before any provider read or mutation is enabled.
