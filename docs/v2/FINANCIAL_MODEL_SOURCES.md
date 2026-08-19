# V1 financial model sources (M5 groundwork)

M5 targets parity with V1 reporting on a canonical, versioned, auditable V2.
This records what the real financial inputs on the server actually are, how V1
reads them, and where the evidence stops. Investigated 2026-08-19 read-only;
the original workbooks were checksummed before and after and are unchanged.

## Where V1 keeps them

Uploaded workbooks live under `DATA_DIR/uploads/financial_models/<asset_id>/`
and are referenced by the `source_files` table, which stores the original
filename, the stored path, SHA-256, MIME type, size and upload time. The stored
name is `<timestamp>_<sha256 prefix>_<sanitized original>`, so a file is
traceable to its row without trusting the filename.

Parsed content lands in `financial_models` (one row per import, with parser
name and version, detected identity, status, `base_year`, warnings, validation
and a `details_json` payload) and `financial_model_monthly` (twelve rows per
model).

Two upload roots exist on this server: the current `data/uploads/` and a legacy
`uploads/` outside `DATA_DIR`. Financial models are only in the current root.

## The real sample is one client and one format

The entire population of financial model inputs on the server is **two files**,
both for asset 2103, both the same client:

| | |
| --- | --- |
| `Financial_Automatic (R3) Expertcom (As Built).xlsm` | 16 521 793 bytes, imported 2026-07-30, `base_year` 2025 |
| `Financial_Automatic (R3) Expertcom (As Sold).xlsm` | 16 521 549 bytes, imported 2026-07-31, `base_year` 2026 |

Both were parsed by `financial_model_workbook` version 3, both detected as
format `financial_automatic_as_sold`, both `status=confirmed` and `active=1`,
both 166.97 kWp, both with the same two warnings. Everything else in the
portfolio has no financial model at all.

Other Excel files on the server are outputs, not inputs: 110 under the legacy
`uploads/generated_reports/1` and 85 under `data/uploads/generated_reports/`.
Those are V1's generated reports and are the parity baseline for output, not
source evidence for parsing.

## As Built and As Sold are financially identical

The two files differ in bytes, so V1 treats them as two models. They do not
differ in any number the parser reads, and they do not differ in any cell value
at all.

Unpacking both OpenXML packages, only four parts differ: `docProps/core.xml`
(document metadata), `xl/vbaProject.bin` (the macro project),
`xl/workbook.xml` (workbook-level settings) and one worksheet,
`xl/worksheets/sheet7.xml`. That worksheet resolves to `Savings Yr1`, and a
cell-by-cell comparison of its full 60 × 25 range finds **zero differing
values**. Every other worksheet is byte-identical.

So for this client the "as built" and "as sold" cases carry the same numbers,
and the distinction that survives into V1 is not in the workbook content. Either
the project was built exactly as sold, or the difference lives in macros and
formatting that no parser reads. A V2 import must not assume the filename
conveys a financial difference.

## What the parser actually reads

The `financial_automatic_as_sold` family is identified by the presence of the
`Projeto` and `Savings Yr1` sheets. Annual and project figures come from fixed
cells on `Projeto`:

| Cell | Meaning |
| --- | --- |
| `C5` | project name |
| `H8` | installed capacity kWp |
| `G39` | access tariff reference year, format `YYYY/n` |
| `H14` | specific yield kWh/kWp |
| `H22`, `H23` | first-year and annual degradation |
| `D26`, `E26`, `D28`, `E28` | installation cost and selling price, per kWp and total |
| `P5`–`P10` | annual consumption, PV, self-consumption, feed-in and the two rates |
| `L32`, `L33`, `F46` | avoided tariff, PPA tariff, surplus sale price |
| `H41`–`H44`, `F41`–`G44` | tariff periods and their energy/network components |

and from `Savings Yr1`: `D16` estimated annual invoice, `F16` energy savings,
`H16` surplus revenue, `D52` total benefit, `F45` grid import.

The twelve monthly rows are read per month, and V1 records the exact source
cell for each field: production from `Projeto!K6`…`K17`, consumption from
`Savings Yr1!C4`…, self-consumption from `E4`…, export from `G4`… with an
adjustment cell at `I4`…

Two monthly fields are not read but derived, and V1 says so explicitly in
`calculated_fields_json` and raises a warning for each:

- `expected_export_kwh` = export minus battery charge
  (`financial_model_battery_export_adjusted`)
- `expected_grid_import_kwh` = consumption minus self-consumption
  (`financial_model_calculated_grid_import`)

This per-field cell provenance plus named derivation rules plus warnings is the
part of V1 worth carrying into V2 verbatim in spirit. It is what makes a number
in a report explainable back to a cell in a customer's workbook.

## base_year does not come only from the file

`Projeto!G39` reads `2025/1` in **both** workbooks, yet the two models carry
`base_year` 2025 and 2026. The import service computes
`effective_year = base_year or parsed.base_year or 0`, where the first term is
an operator value supplied through the upload form. The second model's 2026 is
therefore an operator decision, not file content.

V2 must keep that distinction visible: a base year taken from the workbook and
a base year chosen by an operator are different evidence and should not be
stored as if they were the same.

## Format variation is real, but only anticipated in tests

The documentation and parser support four families, and V1's own test suite
carries fixtures for all of them:

1. **Metric rows by month columns** — a header row of twelve months with rows
   for PV, consumption and self-consumption; `MWh` is converted to `kWh`;
   the `Prod month` sheet wins when present.
2. **Month rows by metric columns** — a month column that may be shifted inside
   the table, with `Prod month` and `Monthly Production` taking priority.
3. **Financial Automatic, UPAC generation** — sheets `UPAC`,
   `Data PV Proposal` and optionally `Detalhes da fatura`. V1's fixture for
   this is named after a different client, Usinage.
4. **Financial Automatic, As sold** — `Projeto` plus `Savings Yr1`, the family
   the two real files belong to.

Within family 4 there are already **two generations**: a regression test covers
a layout where self-consumption and surplus move column, so
`expected_self_use_kwh` comes from `Savings Yr1!F4` instead of `E4`. The parser
locates the tariff period table by its `Período`, `Energia`, `Redes` and
`Total` headers rather than fixed rows, precisely because real files disagree.

That is direct evidence that customer workbooks vary. It is also the limit of
what can be confirmed: the variation is encoded in synthetic fixtures written
from files that are no longer on the server.

## Limitations to state plainly

- **One client, one format, two files.** Nothing on this server proves how a
  second customer's workbook is shaped. Any V2 parser validated only against
  Expertcom is validated against a sample of one.
- **The UPAC and monthly-summary families have no real file here.** Their
  behaviour is known only through V1 code and fixtures.
- **Only 2 of 267 assets have a financial model at all**, so expected-production
  and savings reporting currently rests on a very narrow base.
- The two available files are financially identical, so they do not even
  provide two independent samples of the one format that is present.

Before V2 reimplements this, more real workbooks should be collected from the
customers whose reports V1 already produces, particularly one UPAC-generation
file and one from a client other than Expertcom.
