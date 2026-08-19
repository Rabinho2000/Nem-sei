# Nem-sei V2 device model

M1 decision record. This document derives the canonical device level from the
real V1 data rather than from a preferred diagram, as `GOAL.md` §1 requires. It
is the justification behind the corresponding entries in `DECISIONS.md`.

Evidence gathered 2026-08-18 from the frozen V1 database, opened read-only with
`mode=ro` and `PRAGMA query_only = ON`.

## What V1 actually contains

### One device kind, one provider

All 325 rows in `provider_devices` are FusionSolar inverters. There is not a
single meter, datalogger, string box, or Sigenergy device in the table.

| `dev_type_id` | Devices | Meaning |
| --- | --- | --- |
| 1 | 324 | String inverter |
| 38 | 1 | Residential inverter (SUN2000-6KTL-L1) |

Rated power ranges from 6 kW to 100 kW across 19 distinct SUN2000 models. 324
of 325 devices are enabled; one is disabled.

### Devices cover half the portfolio

133 of 267 V1 assets have devices, holding between 1 and 12 devices each:

```
 1 device  → 57 assets      5 devices →  6 assets
 2 devices → 32 assets      6 devices →  5 assets
 3 devices → 13 assets      8/10/11/12 → 1 asset each
 4 devices → 16 assets
```

### Every device carries three distinct identifiers

All 325 rows have all identifier columns populated — no nulls, no blanks:

| Column | Example | Nature |
| --- | --- | --- |
| `external_device_id` | `1000000141941496` | FusionSolar internal device ID |
| `dev_dn` | `NE=141941496` | FusionSolar distinguished name |
| `sn` | `BT2180195362` | **Hardware serial number** |
| `device_name` | `Inverter 3` | Operator-facing label |
| `model` | `SUN2000-30KTL-M3` | Hardware model |

This is the decisive finding for the canonical model. `external_device_id` and
`dev_dn` are provider artifacts and die with the provider account. The serial
number is a property of the physical hardware: it survives a provider migration,
an account change, and a re-commissioning. A device replaced under warranty gets
a new serial; a plant moved from FusionSolar to another portal keeps it.

`external_device_id` is unique across all 325 rows with zero cross-provider
collisions — but only one provider is present, so this proves nothing about
global uniqueness. Connection-scoped uniqueness, as V2 already enforces for
plants, remains the correct rule.

### Device-to-plant linkage is clean

There are 133 distinct `station_code` values and **zero** that lack a matching
`asset_integrations` row. The device level therefore hangs off the existing
plant claim without any orphan reconciliation work.

### V1 keys device facts four different ways

| V1 table | Rows | Device key | Distinct devices |
| --- | --- | --- | --- |
| `device_realtime_snapshots` | 51 289 | `provider_device_id` (FK) | 325 |
| `inverter_power_samples` | 54 593 | `inverter_id` (TEXT, no FK) | 254 |
| `inverter_availability_sampled_daily` | 6 720 | `provider_device_id` (FK) | 268 |
| `inverter_availability_daily` | 319 | `inverter_id` (TEXT, no FK) | 319 |

All 254 `inverter_id` values in `inverter_power_samples` do resolve to an
`external_device_id`, so the data is consistent — but two of the four fact
tables key device facts on a **provider identifier string with no foreign key**.
That is the concrete V1 debt this milestone exists to avoid inheriting: in V2,
every device fact keys on the canonical device, never on a provider string.

### Temporal machinery exists but has never been exercised

`provider_device_configuration_history` holds exactly 325 rows — one per device,
324 of them still open, and **no device has more than one version**. V1 built
the validity-period structure and never recorded a single device change through
it. The concept is needed; there is no history to migrate.

## Decided model

### Asset stays as the physical installation; Device is its child

No `Plant` layer is introduced between `Organization` and `Asset`.

`GOAL.md` §1 lists Site, Plant, Asset, Inverter, Meter and DataLogger as
candidate entities. The V1 evidence does not support materialising them yet:
`assets` already means one physical installation, every device is an inverter,
and there is exactly one device tier below the installation. Creating empty
`Plant`, `Meter` and `DataLogger` tables now would add joins and constraints
that no row exercises, which contradicts §20 and §21 and gives the next agent
structure to maintain without data to validate it against.

What the model must do instead is refuse to *prevent* those entities. It does
that through an open `device_kind` vocabulary and a self-referencing parent, so
a datalogger owning inverters, or a meter beside them, is an added row rather
than an added migration.

```
Organization
   └── Asset                     (physical installation — unchanged)
         └── Device              (kind: inverter | meter | datalogger | string_box)
               ├── canonical identity: serial_number, model, rated_power_kw
               ├── optional parent_device_id (datalogger → inverters)
               └── provider identifiers via AssetProviderMapping
                        (resource_kind='device')
```

### Devices carry canonical identity separate from provider identity

`devices` holds what belongs to the hardware: serial number, model, rated power,
device kind, lifecycle status, validity period, and an operator label. It does
**not** hold `external_device_id`, `dev_dn`, station codes or payloads. Those are
provider evidence and live in the mapping layer, where they can be superseded
without touching the canonical device.

Serial number is unique per asset, not globally: two installations can plausibly
carry hardware whose serials were transcribed identically, and a global unique
constraint would turn a data-entry error into an import failure. Duplicates
within an asset are a review condition, consistent with how V1 asset-name
duplicates are already quarantined.

### One claim table, extended — not a second mapping table

`AssetProviderMapping` gains a nullable `device_id` and accepts
`resource_kind='device'`, rather than a parallel `DeviceProviderMapping`.

This keeps one uniqueness rule, one supersede/replace mechanism, one audit
surface and one review workflow for every provider claim. The existing
plant-level rules are unchanged: a claim is unique per connection and normalized
external ID, replacements close the previous mapping instead of overwriting it,
and cross-account collisions stay reconciliation conflicts.

A device mapping additionally records the plant mapping it was discovered under,
so device provenance survives a plant remapping and reconciliation can tell an
orphaned device claim from a legitimately re-parented one.

### Device facts key on the canonical device

Every future device-level fact — monitoring observation, current state,
availability, production — references `devices.id`. No V2 fact table repeats
V1's `inverter_id` TEXT pattern.

## V1 to V2 mapping for the importer

| V1 source | V2 target | Rule |
| --- | --- | --- |
| `provider_devices.sn` | `devices.serial_number` | Canonical identity |
| `provider_devices.model` | `devices.model` | Verbatim |
| `provider_devices.rated_power_kw` | `devices.rated_power_kw` | Numeric, non-negative |
| `provider_devices.device_name` | `devices.label` | Operator label, not an identifier |
| `provider_devices.dev_type_id` | `devices.device_kind` | 1 and 38 both map to `inverter`; any other value is quarantined for review rather than guessed |
| `provider_devices.enabled` | `devices.lifecycle_status` | `1` → `active`, `0` → `inactive` |
| `provider_devices.asset_id` | `devices.asset_id` | Only for assets the identity import created; otherwise excluded |
| `provider_devices.external_device_id` | mapping `external_id` | `resource_kind='device'` on the disabled legacy connection |
| `provider_devices.dev_dn` | mapping evidence | Recorded as evidence, not as a second claim |
| `provider_devices.station_code` | parent plant mapping | Resolved to the existing plant claim |
| `provider_device_configuration_history` | not imported | 325 single-version open rows carry no history; V2 starts its own validity periods |
| `provider_device_expected_strings` | not imported in M1 | String-level expectation belongs with performance work, not identity |
| device fact tables | not imported | Facts are M6 work and are regenerated from providers |

The importer reuses the existing evidence machinery unchanged: `row_hash`
fingerprints, `prior_record` replay, batched commits, and the established
`created` / `reused` / `changed_source` / `conflict` / `quarantined` /
`excluded` outcomes. `provider_devices` is treated as an optional source table,
the way `integration_unresolved` already is, so the importer still runs against
a V1 source that lacks it.

Expected reconciliation: 325 devices read, those belonging to the 265 imported
assets created, and every divergence recorded with an outcome. Devices whose
parent asset was quarantined during the identity import are `excluded`, not
silently dropped.

## Deliberately deferred

- Sigenergy devices: the provider exposes none in V1 and its device discovery
  contract is unverified. No speculative schema.
- Meters, dataloggers and string boxes: vocabulary only, no rows, no tables.
- String-level expectations and any device fact: later milestones.
- Device discovery from a provider account: M2 and later, behind the existing
  capability gates.
