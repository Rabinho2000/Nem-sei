# Nem-sei V1 data model

V1 persists to SQLite through `monitoring_board.app_factory` and additive
schema helpers in reporting, portfolio, financial-model, customer, capacity,
and provider repository modules. The schema is an operational V1 contract,
not a V2 model source.

## Main data areas

- Assets and contracts: `assets`, `asset_aliases`, `om_contracts`, capacity,
  customer, and portfolio tables.
- Monitoring and operations: monitoring records/import batches, tickets,
  visits, alerts, and application state.
- Providers: integration configuration, asset mappings, sync runs, realtime
  snapshots, provider devices, Sigenergy operations, and API queue state.
- Energy and performance: production records, hourly facts, availability,
  inverter samples, expected production, and performance settings.
- Reporting and finance: financial models, tariffs, invoices, templates,
  snapshots, generated files, distribution records, and portfolio report runs.
- Background work: `background_jobs` with V1-specific pending/running/waiting
  states and recovery metadata.

The V1 schema is initialized by idempotent `CREATE TABLE`, `ALTER TABLE`, and
index helpers. It must not be upgraded by V2 Alembic or opened for V2 writes.

Any future migration reads the V1 database in read-only mode, maps each domain
explicitly, and validates counts, natural keys, totals, quality states, and
referential integrity before accepting V2 data.
