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
| M5.4 | Expected production, availability, data quality and the quality gate | **partly done**: quality rules ported, availability and energy facts pending |
| M5.5 | `ReportingDataset` and `ReportSnapshot`, reproducible from persisted facts alone | |
| M5.6 | Excel and PDF rendering, visual and numerical parity | |
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
