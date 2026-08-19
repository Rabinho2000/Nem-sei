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
| M5.2 | Financial models persisted and versioned in PostgreSQL: source file, hash, cell provenance, derivation rules, warnings, base-year origin | next |
| M5.3 | Commercial rules: tariffs, ESCO/EPC, billing, client outages | |
| M5.4 | Expected production, availability, data quality and the quality gate | |
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
