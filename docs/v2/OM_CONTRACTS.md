# O&M contracts: which installations Solcor operates

V1 could answer "does this plant have O&M?" and V2 could not. This documents
what the answer was made of, what was carried over, and what was deliberately
left behind.

## What the question was in V1

Five columns across two tables, with no history:

| V1 | meaning | populated |
| --- | --- | --- |
| `assets.maintenance` | the scope: we do O&M here | 92 `yes`, 172 `no`, 3 empty |
| `assets.active_contract` | shown as "O&M" in the interface | 83 `yes`, 184 `no` |
| `assets.start_contract` / `end_contract` | the contract period | 105 / 91 |
| `assets.duration` | years | 237, of which 132 are the string `"0"` |
| `om_contracts` | `UNIQUE (asset_id)`; annual value (2), notes (2), PDF path (2), `renewal_status` (0) | 7 rows |

**`active_contract` was never independent data.** `derive_active_contract`
computes it from `end_contract >= today`, and `sync_all_contract_statuses` kept
the stored copy from drifting. Verified against the frozen snapshot: of the 91
scoped installations carrying an end date, the derivation reproduces V1's 83
`yes` and 8 `no` exactly, and the one scoped installation with no dates (V1
asset 2100, Jetesetecar Expansão) falls through to its stored value. The
importer re-runs that check every time and reports mismatches; the production
dry run reported **0 mismatches over 91 checks**.

## What V2 stores

One table, `asset_service_contracts`: one engagement, one installation, one
date range. Renewal is a new row, so the terms that were true last year survive
— V1's `UNIQUE (asset_id)` destroyed them. A GiST exclusion constraint refuses
two rows claiming the same day for the same installation.

Everything else is derived on read by `nemsei/contracts/service.py`:

| derived status | meaning |
| --- | --- |
| `active` | a period covers the date asked about |
| `expired` | the most recent period ended before it |
| `undated` | operated, but no period was ever written down |
| `none` | no engagement was ever recorded |

Nothing is scheduled and nothing drifts: a contract that lapses at midnight is
lapsed at midnight.

Two conversions matter and are both auditable in `provenance_json`:

- **V1's `end_contract` is inclusive, V2's `valid_to` is exclusive**, so the
  importer adds one day — the same correction `v1_reporting_import` already
  applies to `asset_tariffs.valid_to`. `v1_end_inclusive` records what V1 said.
- **Dates come from `om_contracts` first, `assets` second**, which is V1's own
  `COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, ''))`.

## Scope, and what it gates

Two different questions, which V1 conflated under one word:

- **In scope** (`assets_in_om_scope`) — any engagement, live or lapsed. This is
  V1's `maintenance`: 92 installations. A lapsed contract does not remove a
  plant from the O&M portfolio, it makes it a renewal.
- **Under contract** (`assets_with_active_om`) — a period in force today. This
  is V1's `active_contract`: 83, plus the one `undated` installation, which is
  operated with the paperwork missing rather than not operated.

`notification_policies.asset_scope` (`all` / `om` / `om_active`) restores V1's
`ALERT_SCOPE`, which V2 did not have at all: a policy switched on before this
would have alerted for all 267 installations.

**The chosen default is `om_active`, and it has a consequence worth stating.**
An installation whose contract lapses stops generating alerts that day. That is
deliberate — it was the operator's decision, recorded 2026-08-25 — and it is
precisely why the renewals screen and the panel tile exist: the lapse has to be
loud somewhere, because it is no longer loud in the alert channel.

## What was not carried over

| V1 | why |
| --- | --- |
| `assets.active_contract` | derived, not data — see above |
| `assets.duration` | 132 of 237 values are `"0"`; kept as provenance, never as a bound |
| `om_contracts.pdf_path` | the operator's ruling: the two referenced files do not exist on disk (`uploads/contracts/` is empty) and the references were not worth carrying |
| `om_contracts.renewal_status` | zero rows populated in V1; V2 defines its own vocabulary and starts clean |

## Import

```text
python -m nemsei.contracts.v1_import \
  --source <frozen-v1.sqlite> --operator <name> --as-of <date> [--apply]
```

Read-only against V1, idempotent (keyed on `v1_asset_id` in the provenance),
dry run unless `--apply`. The manifest carries the source SHA-256, the counts
and every issue.

### Production dry run, 2026-08-25

Source `7c981525…0674`, verified against the real V1 snapshot with the
production identity map:

| count | value |
| --- | --- |
| contracts created | 92 |
| out of scope | 175 |
| scoped but never imported into V2 | 0 |
| needs review | 4 |
| derivation checks / mismatches | 91 / **0** |

The four review flags: V1 assets 2100 (no dates at all) and 2115 (end date
only), plus 1900 (Vatel) and 1985 (Banco BIG), whose V1 rows start and end on
the same day. Because V1's end is inclusive those are one-day contracts —
representable, so imported exactly as stated, and flagged rather than rewritten.
