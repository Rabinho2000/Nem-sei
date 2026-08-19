# Nem-sei V2 decisions

- Flask and direct SQLAlchemy are retained; Flask-SQLAlchemy is not used.
- PostgreSQL is the only V2 operational database. SQLite is limited to frozen
  V1 and the V1 importer's read-only source/fixtures.
- Alembic is the only schema migration mechanism; web does not auto-migrate.
- Runtime data and Git worktrees are isolated from V1.
- External capabilities default to deny.
- Jobs are at-least-once and all future side effects require explicit
  idempotency strategies.

## Canonical device level (M1)

Justification and V1 evidence: `DEVICE_MODEL.md`.

- `assets` remains the physical installation. No `Plant` tier is introduced
  between `organizations` and `assets`, because V1 has exactly one device tier
  below the installation and every one of its 325 devices is an inverter.
- `devices` is a child of `assets` with an open `device_kind` vocabulary
  (`inverter`, `meter`, `datalogger`, `string_box`) and an optional
  `parent_device_id`. Only `inverter` is populated by migration; the remaining
  kinds are enabled by adding rows, not by adding tables.
- Canonical device identity is the hardware serial number, model and rated
  power. Provider identifiers (`external_device_id`, `dev_dn`, station codes)
  are provider evidence and live in the mapping layer only.
- Serial numbers are unique per asset, not globally. A duplicate within an asset
  is a review condition, never an import failure or an automatic merge.
- `asset_provider_mappings` is extended with a nullable `device_id` and accepts
  `resource_kind='device'`; there is no separate device mapping table. Existing
  plant claim, supersede and conflict rules are unchanged. A device mapping
  records the plant mapping it was discovered under.
- Every device-level fact references `devices.id`. No V2 fact table keys a
  device on a provider identifier string, which is V1's pattern in
  `inverter_power_samples` and `inverter_availability_daily`.
- `provider_device_configuration_history` is not imported: its 325 rows are all
  single-version and open, so there is no device history to migrate.

## Future side-effect idempotency contracts

Every side-effecting handler must persist an idempotency key before invoking an
external system, define replay behavior, and record an uncertain external
outcome for reconciliation rather than blindly repeating the call.

| Future operation | Idempotency key | Replay behavior | Uncertain external outcome |
| --- | --- | --- | --- |
| Production/history import | provider, external reference, observed period, fact version | Durable natural-key upsert | Re-fetch and reconcile by external reference/period |
| Report generation | approved snapshot or artifact specification/version | Return the existing immutable artifact | Retain pending artifact state and regenerate only from the same snapshot |
| Report distribution | snapshot, artifact, recipient, channel, template version | Return the existing delivery record | Query provider delivery status where available; otherwise mark for manual reconciliation |
| Notification | business event, destination, template version | Reuse the durable delivery record | Mark delivery uncertain; never silently resend |
| Provider mutation | persisted operation key and provider idempotency key | Return recorded provider outcome | Query provider operation/status before a retry; require manual reconciliation if unavailable |
