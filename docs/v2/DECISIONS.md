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

## Reporting inputs (M5, Fatia B)

Evidence: V1's own `production_records`, `asset_tariffs`, `asset_billing_configs`
and `assets`, surveyed read-only on 2026-08-19.

- **The canonical energy vocabulary is five metrics, not one.**
  `production_facts.metric_kind` accepts `production_energy`, `self_use_energy`,
  `export_energy`, `consumption_energy` and `grid_import_energy`. Each is a
  signal a provider states; none is stored as a derived value.
- **No metric is ever derived from another.** The identity
  `production = self_use + export` holds exactly on 40 303 of 41 503 FusionSolar
  daily rows, and does **not** hold for Sigenergy, where a battery absorbs the
  difference. Deriving would have invented a number for every plant that stores
  energy. The identity is used only to reject the impossible: a row whose export
  exceeds its production is persisted with `quality='invalid'`, carrying the
  value it claimed, rather than being dropped or silently corrected.
- **Live reads and historical import are gated separately.** The FusionSolar
  adapter still accepts only the operator-verified `PVYield` for live calls.
  The additional metrics enter V2 through the V1 importer, which reads payloads
  V1 already stored, and every fact records `origin: v1_production_records` in
  its metadata. Verifying the live contract for the other signals is separate
  work and does not block reporting.
- **Tariffs and billing configuration are temporal, and the database enforces
  it.** A GiST exclusion constraint refuses two rows covering the same day for
  one asset. V1 permitted the overlap and resolved it by taking the newest row,
  which is how a customer can be billed at a price nobody chose. This is the one
  place a contrib extension (`btree_gist`) was added, because the guarantee
  belongs in the database rather than in a code path someone can forget.
- **Prices are `Numeric(28, 18)`.** V1 stores them as TEXT and parses through
  `Decimal`; its one real billing row carries seventeen decimal places.
- **Contract attributes are free text, deliberately.** `contract_type`,
  `asset_type`, `coverage_type` and `sell_to` are copied as V1 wrote them,
  because normalising would lose distinctions its operators made ("EPC (O&M)",
  "ESCO BUYOUT"). Report type is resolved from them by the already-ported rule.
- **A defaulted report type is reported as defaulted.** `detect_report_type`
  answers EPC both when it reads "EPC" and when it reads nothing.
  `report_type_resolved` and `report_type_source` separate the two, because
  sending an ESCO customer an EPC document is a commercial error, not a
  formatting one.

