# V2 reporting parity with V1 (M5)

M5 rebuilds V1's reporting on canonical, versioned, auditable V2 foundations,
with V1 as the source of truth for rules, calculations and formats. No redesign:
parity first.

## The size of the thing

V1's reporting logic is **11 264 lines across roughly twenty modules**. It is by
some distance the largest surface in the product, and it does not port in one
pass:

| Area | V1 modules |
| --- | --- |
| Financial models | `reporting/financial_models.py` (1 012), `financial_quality.py` |
| Datasets and repositories | `reporting/repositories.py` (1 207), `reporting/models.py` |
| Portfolio reporting | `portfolio_reports.py` (1 194), `reporting/portfolio.py`, `services/portfolio_reporting.py` |
| Customer reporting | `customer_reports.py` (907) |
| Energy and availability | `services/energy_facts.py` (878), `services/sampled_availability.py` (847) |
| Rendering | `services/report_rendering.py` (763), `reporting/templates.py` |
| Snapshots and periods | `reporting/snapshots.py` (475), `reporting/periods.py`, `monthly_close.py` |
| Commercial rules | `reporting/tariffs.py` (388), `billing.py`, `invoices.py`, `client_outages.py` |
| Quality | `reporting/quality_gate.py` (278), `data_quality.py`, `validation.py` |

## Phases

Ordered so each phase is provable against real V1 evidence before the next
depends on it.

| Phase | Content | State |
| --- | --- | --- |
| **M5.1** | Financial model parsing with full provenance, golden parity on the real workbook | **done** |
| M5.2 | Financial models persisted and versioned in PostgreSQL: source file, hash, cell provenance, derivation rules, warnings, base-year origin | **done** |
| M5.3 | Commercial rules: tariffs, ESCO/EPC, billing, client outages | **done** |
| M5.4 | Expected production, availability, data quality and the quality gate | **done for what V2 can hold**: quality rules and the availability calculation ported; the rest needs device-level facts |
| M5.5 | `ReportingDataset` and `ReportSnapshot`, reproducible from persisted facts alone | **done** |
| M5.6 | Excel and PDF rendering, visual and numerical parity | **done for the asset report**; portfolio reports wait on V2 portfolios |
| M5.7 | End-to-end golden tests V1 versus V2, with every difference explained | |

## M5.1: financial model parsing

V1's parser is self-contained: it imports only openpyxl and the standard
library, and touches neither the V1 application nor its database. It was
therefore ported verbatim into `src/nemsei/reporting/financial_workbook.py`
under V2 ownership, so V2 reproduces V1's numbers cell for cell rather than
approximating them. `openpyxl==3.1.5` was added to the V2 lock at the same
version V1 parses with.

Porting rather than rewriting is deliberate. Parity is the requirement; a
cleaner parser that produces different numbers would fail the milestone, and any
future change to this module must be justified against a real workbook.

### Golden evidence

`tests_v2/test_financial_workbook_golden.py` parses the real Expertcom
`As Sold` workbook from the V1 uploads directory and compares against what V1
persisted for it. It asserts:

- identity and parser contract: detected name, capacity, sheet, parser name and
  version, and the workbook's own base year of 2025;
- **every monthly value**, across all twelve months and all five energy fields,
  to a relative tolerance of 1e-9;
- **provenance**, field by field: the same source cell for each value and the
  same named derivation rule for each calculated field;
- warnings and all six detail sections, including tariff periods, electricity
  costs, invoice periods, invoice prices and invoice totals;
- that a missing value stays missing and never becomes a zero;
- that the workbook's SHA-256 is unchanged after parsing.

All four tests pass against the live evidence. They skip cleanly where that
evidence is absent, including inside the test container, because V1's live
SQLite runs in WAL mode and cannot be opened from a read-only mount. Neither the
16 MB workbook nor the customer's financial values are committed to Git; the
comparison runs against the server's own copies.

To run the golden tests where the evidence exists:

```bash
PYTHONPATH=/opt/server/apps/Nem-sei-v2/src /opt/server/apps/Nem-sei/.venv/bin/python \
  -m pytest tests_v2/test_financial_workbook_golden.py --noconftest --rootdir=/tmp
```

### What is validated, and what is not

Only the `financial_automatic_as_sold` family has a real workbook to validate
against, and the two files on the server are financially identical, so the
sample is effectively one. The other three families V1 supports are carried over
in the same module and remain **unvalidated against any real file**; their only
coverage is V1's synthetic fixtures. `FINANCIAL_MODEL_SOURCES.md` records that
survey, including the two layout generations V1 already had to accommodate
within the as-sold family.

Any V2 report that depends on a family other than as-sold must therefore be
treated as unproven until a real workbook for it is collected.

## M5.2: financial models in PostgreSQL

Migration `0010_financial_models` adds three tables. `report_source_files`
records one uploaded artefact per content hash, so the same workbook cannot be
registered twice or claimed by two assets. `financial_models` holds one parse,
versioned per asset, and `financial_model_months` holds its twelve expected
months.

Provenance is not a side note here, it is the schema. Each model keeps the
source file's SHA-256, the parser name and version, the warnings, the full
detail payload and the source-cell map. Each month keeps the cell behind every
value and the named rule behind every derived one, so a number in a report leads
back to a cell in the customer's workbook.

Two deliberate departures from V1, both recorded rather than silent:

- **`base_year_source`.** V1 stored the workbook's year and an operator's choice
  in the same column, which is how the same Expertcom file appears as 2025 and
  2026. V2 records which one it was, and the cell when it came from the
  workbook. A database constraint refuses an operator-chosen year that does not
  name its actor.
- **Numeric rather than float.** Monthly values are `Numeric(20, 10)` so sums are
  exact. The scale is deliberately generous: the workbook holds IEEE-754 values
  with about a dozen decimals, and rounding them away would break numerical
  parity.

### A V1 rule worth stating

A workbook missing any month is **refused in full** with
`financial_model_missing_month`; V1 does not store a null month and neither does
V2. A financial model drives a whole year of expected production, so a month the
workbook does not state cannot be guessed, interpolated or defaulted. The import
raises and nothing reaches the database, which is the strongest possible form of
"never turn missing into zero".

Migration 0010 was rehearsed against a restored copy of the live V2 database:
upgrade and downgrade both clean, with the 266 assets, 325 devices and existing
production facts untouched. Its downgrade refuses to run if any financial model
exists, so imported provenance cannot be dropped by accident.

## M5.3: commercial rules

V1's tariff, billing, invoice and client-outage modules depend only on their own
dataclasses and the standard library: no database, no Flask, no provider. They
ported cleanly into `src/nemsei/reporting/rules/`, with V1's
`reporting/models.py` becoming `rules/types.py` because V2 already uses
`reporting/models.py` for persistence.

### Golden parity, both implementations side by side

`tests_v2/test_commercial_rules_golden.py` loads the frozen V1 modules and runs
them against V2's over the same inputs, asserting identical results including
identical failures. Thirty-eight cases pass:

- self-use inference, including export larger than production;
- decimal normalisation across European and US separators, grouped thousands,
  empty and invalid input;
- Portuguese NIF validation and normalisation;
- tariff window membership, including a window that crosses midnight;
- tariff type parsing;
- and the enum vocabularies themselves, so a silently renamed member cannot
  change reports without failing a test.

### The V1 import boundary, kept honest

`test_v2_never_imports_v1` now exempts files named `*_golden.py`, because a
parity test has to load the reference it compares against. The exemption is
declared rather than worked around, and it is paired with a new check that no
V2 **source** file reaches V1 dynamically either: the source tree may name V1 in
a docstring, which several ports usefully do, but never in a string the code
could act on.

## M5.4 in part: the quality rules

`quality_gate.py`, `data_quality.py` and `validation.py` carry no SQL at all, so
they ported into `rules/` like the commercial ones. These are the rules that stop
a wrong number reaching a customer, so parity is asserted on the whole verdict
object rather than on a summary: thirty-one cases compare V1 and V2 findings
field by field across seven month shapes, a mid-month and an after-close
reference date, both report scopes, and both expected-production settings.

Two modules of this phase remain: `services/energy_facts.py` and
`services/sampled_availability.py`. Unlike everything ported so far they speak
SQLite directly, with 24 and 46 lines of query code, so they cannot be copied.
Their calculations have to be separated from their persistence before they can
be carried over, which is the next slice rather than a rewrite done in passing.

## M5.5: datasets and snapshots

A `ReportingDataset` resolves one asset's reporting period from persisted facts
alone, pairing actual production from `production_facts` with expected
production from the confirmed financial model, month by month. A
`ReportSnapshot` freezes a dataset and the payload computed from it.

Reproducibility is enforced by content, not by convention. The dataset hashes
its resolved values and their provenance, deliberately excluding build time and
the operator's name, so rebuilding from the same facts produces the same
`input_digest` and adding one fact changes it. The snapshot hashes the dataset
digest together with its payload, so freezing identical input returns the
existing snapshot instead of creating a second one.

Missing is a first-class state, in the schema rather than only in code. A month
with no fact is `missing` with a null value, a month with some unusable facts is
`partial`, and database constraints refuse a row that claims to be missing while
carrying a number. `report_snapshots` is append-only at the database level,
because a snapshot is the record of what a customer was told: update and delete
both raise.

A test reads the dataset module's own source and asserts it contains no
integration, HTTP or provider reference at all, so the report path stays
answerable from the database alone.

## M5.4 concluded: availability

Only the calculation itself is portable. `weighted_sampled_availability` weights
each device by rated power and falls back to a plain mean the moment any device
has no usable rating, rather than treating an unknown rating as zero weight.
Twenty golden cases pin it against V1, including the one that decides customer
arguments: 90 kW at 100 % beside 10 kW at 0 % is 90 %, not 50 %.

A device with unknown availability makes the plant's availability unknown. It is
never counted as zero, because "one inverter did not report" and "one inverter
was down all day" are different statements about a customer's plant.

The rest of V1's `sampled_availability.py` is ten SQLite functions over inverter
samples and device realtime snapshots, and V2 holds neither: devices exist as
canonical identity but carry no facts yet. Persisting availability therefore
waits for device-level facts rather than being ported against tables that do not
exist. The same applies to `energy_facts.py`, whose pure half is entirely
Sigenergy history parsing, for a provider whose production contract is still
unverified.

## M5.6 started: the Excel document

`src/nemsei/reporting/excel.py` renders an asset report from a payload alone,
with no database and no provider, and its structure is taken from a real V1
output on the server rather than invented: the same five sheets in the same
order, the same metric rows, the same metadata fields, and V1's summary layout
including its blank spacer rows and the `Instalação: … | Período: …` line in A4.

Two findings changed the code rather than the test:

- **Float formatting, not Decimal.** V1 formats floats, so 0.005 renders as
  0.01. Formatting through `Decimal` applies banker's rounding and would have
  written 0.00. The difference is one cent on a boundary, which is exactly the
  kind of difference a customer notices and nobody can explain later.
- **V1 has several rendering paths, not one.** `report_rendering.py` drives
  Excel through templates with per-section row builders, and they do not agree
  on how a missing value appears: some sections leave the cell empty, others
  write the literal `Dados indisponiveis`, and `str(report.get(key, default))`
  writes the string `None` when a key exists holding None. The current V2 writer
  reproduces the blank-cell convention seen in the real artefact.

So this phase is honestly started, not finished. What is pinned is the sheet
structure, the row vocabulary, the number formatting and the rule that a missing
value never becomes a zero while a real zero stays a zero. What is not yet
ported is V1's template engine, its fonts, fills, merged cells and column
widths, its per-section placeholder conventions, and PDF output. Portfolio
reports additionally wait on V2 having portfolios at all.

## M5.6 concluded for the asset report

**Excel, against a real artefact.** The writer now matches a V1 output on the
server cell for cell and style for style: the same five sheets in order, the
same row vocabulary, the same metadata fields, V1's merged ranges, its column
widths, its 30-point title row, and its palette of `0B2D52` and `4BA52E` with
the exact font weights and sizes per banner. Twelve tests compare V2's workbook
against the reference rather than against a fixture written from the same
assumptions as the code.

**PDF, against V1 itself.** `customer_reports.py` turned out to be portable for
the same reason the parser was: its only V1 dependencies were the billing rules
and their dataclasses, both already under V2 ownership, and an optional logo
path the caller supplies. It moved across as a copy, and `reportlab==4.4.10` was
added to the lock at V1's version.

The golden test draws the same report with both implementations and compares
page geometry, page count and extracted text page by page. Bytes cannot be
compared because reportlab stamps a creation time into every file, so the
comparison is on what a reader sees. Four payloads pass identically: an EPC
report, an ESCO report, one where every value is missing, and one where
production is a real zero. The missing and zero cases are also asserted to
render differently, because two implementations agreeing on a wrong answer would
still pass a pure parity check.

**A guard that did its job.** The architecture test already caught dynamic
`importlib.import_module` calls, not only static imports, so it flagged the new
PDF parity test immediately. The fix was to rename the file to `*_golden.py`,
which is the exemption this repository declares, rather than to loosen the
check.

## What remains

`ReportingDataset` and the renderers meet in the middle but the end-to-end
golden test of M5.7 is not yet written, and it cannot be meaningful until V2
holds enough real facts to fill a report: today it has two production facts from
the canary. Portfolio reports additionally require portfolios, which V2 does not
have, and the portfolio artefact on the server needs 46 columns V2 cannot
currently produce a single one of.
