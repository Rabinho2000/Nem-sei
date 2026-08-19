# Portfolios: what V1 holds, and what V2 builds

V1 is treated here as a source of data and historical evidence only. Its
portfolio pages are not a reference for how V2 presents anything.

## Inventory of V1, surveyed read-only on 2026-08-19

Eight tables carry the concept. Only four hold rows.

| Table | Rows | What it is |
| --- | --- | --- |
| `portfolio_groups` | 2 | The portfolios themselves |
| `portfolio_assets` | 80 | Membership, one row per sub-account |
| `portfolio_mapping_events` | 59 | An audit trail of manual mapping decisions |
| `portfolio_report_profiles` | 5 | Column selections for reports |
| `portfolio_report_runs`, `portfolio_report_rows`, `portfolio_import_runs`, `portfolio_report_profile_versions` | 0 | Never used |

### The two portfolios

`Solcorelios I` (47 members) and `Solcorelios II` (33 members), both created
2026-06-26, both active, neither archived, neither with a description.

### What a membership row actually is

Not simply "asset X is in portfolio Y". Each row carries `external_name`, `nif`
and `sub_account`, and the sub-account is the member's identity **within** the
portfolio: every one of the 80 rows has a distinct sub-account inside its
portfolio. `asset_id` is the link to an installation *where one exists*.

That distinction matters, because 23 of the 80 rows have no `asset_id`:

| Mapping state | Rows | Meaning |
| --- | --- | --- |
| `manual` / confidence 1.0 | 57 | An operator linked this member to an installation |
| `mapping_pending` | 18 | A real company with a NIF, not yet linked |
| `missing_source` | 5 | Placeholders, literally named "Subconta 00N - importar mais tarde" |

**All 55 distinct V1 asset ids used by these portfolios resolve to V2 assets**
through the identity import's own records. Nothing is lost by importing.

Of the 18 pending members, **7 have a NIF that exactly matches a V2
organization**. A NIF match names the customer, not the installation, so it is
recorded as evidence for an operator to act on and never turned into membership
automatically.

Membership is many-to-many and overlapping: 2 assets and 4 NIFs appear in both
portfolios. There is no nesting in V1 and none is introduced.

### The metric vocabulary V1 reported

The default profile selects 30 columns, and they are the honest requirement list
for a portfolio dataset:

`installation, local_installation, nif, sub_account, installed_power_kwp,
mapping_confidence, actual_production_kwh, helioscope_expected_kwh,
adjusted_expected_kwh, deviation_kwh, deviation_pct, specific_yield,
availability_pct, self_use_kwh, export_kwh, consumption_kwh, grid_import_kwh,
self_consumption_rate_pct, self_sufficiency_rate_pct, estimated_value_eur,
export_revenue_eur, esco_payment_eur, fixed_fee_eur, net_benefit_eur,
invoice_status, tariff_type, data_status, coverage_pct, warning_count,
warning_labels`

After M5 Fatia B, V2 can produce all but four of these per asset from persisted
data. The exceptions are `availability_pct` (needs device-level facts),
`helioscope_expected_kwh` (V2 uses financial models as its expected source),
`invoice_status` (no invoice workflow in V2) and `mapping_confidence` (a V1
import artefact, preserved as membership provenance rather than a metric).

## The V2 design

### Model

- **`Portfolio`** — a named, owned collection. Flat: a portfolio has no parent
  and cannot contain another. Optionally scoped to one `Organization`, which is
  what "portfolio por cliente" means.
- **`PortfolioMembership`** — temporal. An asset belongs to a portfolio over a
  date range, so a portfolio's composition in March is answerable in December.
  It also carries the V1 sub-account and external name, because a member that
  has no asset yet is still a member with an identity.
- **`PortfolioRule`** — a dynamic filter over asset attributes: country,
  lifecycle status, provider, contract type, owner. Rules *select* assets; they
  never create sub-portfolios. Country and provider are filters, by design.
- **`PortfolioSnapshot`** — freezes the exact asset list for a period, so a
  report issued in March keeps naming the plants it actually covered even after
  membership changes. Append-only, like `report_snapshots`.
- **`PortfolioDataset`** — the single source for dashboard, reporting and
  exports, built by aggregating the per-asset `ReportingDataset` that M5 already
  produces. No metric is recalculated here; the aggregation reuses the
  individual reporting or it is a bug.

### Rules that the database enforces

- No portfolio inside a portfolio: there is no parent column to hold one.
- One membership per asset per portfolio per period, through a GiST exclusion
  constraint on the validity range, the same mechanism tariffs use.
- A snapshot never changes, enforced by trigger.

### Reuse, not duplication

`PortfolioDataset` builds one `ReportingDataset` per member asset and sums what
is summable, counting what is not. A missing metric on one asset makes the
portfolio total *partial* rather than silently smaller — the same distinction
between missing and zero that the individual reporting already enforces.

That is also what makes batch reporting and monthly automation a scheduling
problem rather than a calculation problem: the aggregate and the individual
reports come from the same datasets.

### Import

Idempotent and auditable. Portfolios and memberships are keyed by their V1 ids
in provenance, so a second run creates nothing. Members with no `asset_id` are
imported as unresolved members with their NIF and sub-account preserved, never
guessed into an asset. NIF matches are recorded as candidate evidence for an
operator, which is the opposite of destructive fuzzy matching.

### UI

Rebuilt from scratch under `web/templates/portfolios/`, in V2's own shell. V1's
portfolio pages are not consulted. Sections: **Overview, Instalações, Produção,
Disponibilidade, Financeiro, Relatórios, Configuração**. The overview answers
one question first — which installations need attention — and country, region
and provider appear as filters rather than as structure.

## What the import actually did

Applied 2026-08-19 against the live V2 database, after a dry run that matched the
inventory exactly:

| | |
| --- | --- |
| Portfolios created | 2 |
| Members created | 80 |
| Resolved to a V2 asset | 57 |
| Unresolved, evidence preserved | 18 |
| Placeholders | 5 |
| Members lost | **0** |
| NIF candidates raised for review | 8 |

Re-running creates nothing: 2 portfolios and 80 members skipped.

Aggregated for July 2026, from the individual reports:

| | Solcorelios I | Solcorelios II |
| --- | --- | --- |
| Members | 47 (35 with an installation) | 33 (22 with an installation) |
| Coverage | 25/35 complete | 14/22 complete |
| Installed | 4 666.64 kWp | 1 564.96 kWp |
| Production | 133 559.89 kWh (partial) | 67 442.08 kWh (partial) |
| Self-consumption | 76 821.72 kWh (18/35) | 31 144.15 kWh (10/22) |

Expected production is missing for both, so performance is not stated: it needs
a confirmed financial model per asset, and these assets have none yet.

## Batch reporting and monthly automation

The unit is already in place. Freezing a period and aggregating it is one call
per portfolio — `freeze_snapshot` then `build_portfolio_dataset` — and both are
idempotent: an unchanged membership reuses its snapshot, and a rebuilt dataset
over unchanged facts produces the same `input_digest`. A monthly job therefore
needs scheduling, not new calculation, which is what M9 will wire up.

## Reviewing and resolving unresolved members (added 2026-08-19)

The settings page's original "type an asset id" form asked an operator to know
an installation's numeric id by heart, which is neither simple nor auditable in
practice. `/portfolios/<id>/members/review` replaces it: one card per open,
unresolved member, each with whatever evidence exists — an exact NIF match
against a V2 organization (which names the *customer*, never the installation,
so it lists that customer's own assets to choose from) and a name search over
assets using the same `asset_search_clause` the rest of the product searches
with. Nothing on the page resolves a member by itself; `resolve_member_to_asset`
is still the only path from `unresolved` to `resolved`, and it still takes an
explicit id and records who chose it.

Resolving to an asset already claimed by an overlapping membership in the same
portfolio is rejected with a plain message rather than a raw database error —
the exclusion constraint that stops a plant being counted twice now surfaces as
something an operator can read.

## The monthly workflow (added 2026-08-19)

`portfolio_report_runs` and `portfolio_report_run_members` (migration
`0014_portfolio_report_runs`) turn "the numbers for July" into an operational
decision: **gerar -> rever -> aprovar**, sitting on top of the coverage check
("Construir") that already existed.

- **Generating** rebuilds the aggregate, then produces an individual report for
  every member with an asset and any production fact in the period, through
  `assemble_asset_report` + `snapshot_dataset` — the exact path an individual
  report uses standing alone. A member with zero production facts is `blocked`
  with a stated reason rather than handed an empty document.
- **Regenerating** before approval is safe and expected: it replaces the run's
  members against whatever facts exist now, and sends a `reviewed` run back to
  `generated`, because a review is a statement about the numbers it was shown
  and stops being true the moment they change.
- **Approving** is final. A database trigger refuses to update or delete an
  approved run, or any of its members, mirroring the guarantee
  `report_snapshots` and `portfolio_snapshots` already give the records
  beneath it. No distribution happens from this layer — that is deliberately
  later work — but a scheduler will eventually read exactly this state to
  decide whether a period needs attention at all.

### Two structural bugs this surfaced, fixed at the cause

Wiring the workflow to real HTTP requests — not just to unit tests calling the
service functions directly — found two bugs neither prior test had reached:

1. **`snapshot_dataset`'s own promise was false.** Its docstring says
   "re-freezing identical input reuses it," but the digest it computed included
   `payload["dataset_id"]`, the primary key of a `ReportingDataset` row that is
   never deduplicated — two builds of the same unchanged facts get two
   different ids. Every regeneration therefore looked like new content, and a
   customer would receive a report reissued from scratch every time it was
   asked for again. The digest now excludes that one bookkeeping field; the
   stored payload still keeps it for provenance. `test_report_assembler.py`
   pins the fix by assembling and freezing a real period's report twice.
2. **A period-end convention mismatch made a freshly generated run invisible.**
   `PortfolioSnapshot` and `PortfolioDataset` both key a period by its
   **exclusive** end (`exclusive_end(period)`); `generate_report_run` wrote the
   run with `ReportingPeriod`'s own **inclusive** end. The row existed, but
   every subsequent lookup — including the one the "reports" tab itself uses —
   searched for the wrong date and found nothing, so a portfolio's own
   dashboard showed "not generated yet" for a period that had just been
   generated. Caught only by a browser-level test that generated a run and
   then reloaded the page, exactly as an operator would.

Also fixed: `resolve_member_to_asset` now catches the exclusion-constraint
violation from resolving into an asset that already overlaps, inside a nested
transaction, and turns it into a clear `ValueError` instead of leaving a raw
`IntegrityError` to abort whatever the caller's outer transaction was doing.

