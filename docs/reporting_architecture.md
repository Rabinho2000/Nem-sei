# Reporting Architecture Audit

This document maps the current reporting implementation and proposes an incremental architecture for `monitoring_board/reporting/`. It is intentionally a planning artifact: no business behavior should change in Phase 0.

## Current Entry Points

### Individual Customer Report

- UI route: `exports()` in `monitoring_board/app_factory.py`.
- Template: `templates/exports.html`.
- Data builder:
  - `build_fusionsolar_customer_production_report()`
  - `build_local_customer_production_report()`
  - `normalize_customer_kpi_row()`
  - `normalize_customer_production_record()`
- PDF response:
  - `export_customer_production_pdf()` in `app_factory.py`
  - `build_customer_report_pdf()` in `monitoring_board/customer_reports.py`

Current flow:

1. `/exports` validates `asset_id`, month, prices and `force_api`.
2. It calls `build_fusionsolar_customer_production_report()`.
3. Unless `force_api` is set, local `production_records` are preferred.
4. If no local data exists, FusionSolar day and month KPI endpoints are queried directly from `app_factory.py`.
5. Production, self-use, export and consumption are normalized from provider payloads.
6. `prepare_customer_report()` calculates EPC/ESCO financial values and percentages.
7. `build_customer_report_pdf()` renders one monthly PDF page.

### Portfolio Report

- UI routes:
  - `portfolios()`
  - `portfolio_reports()`
  - `generate_portfolio_report()`
  - `export_portfolio_report()`
  - `export_portfolio_mapping()`
- Templates:
  - `templates/portfolios.html`
  - `templates/portfolio_reports.html`
- Main module: `monitoring_board/portfolio_reports.py`.

Current flow:

1. `/portfolios` selects portfolio, month, tab and warning filters.
2. `build_portfolio_report_rows()` reads portfolio assets and computes each row.
3. `aggregate_portfolio_total()` creates the total row.
4. `build_portfolio_kpis()` builds top-level KPIs.
5. `filter_report_rows()` filters by missing data, invoice, Helioscope or mapping.
6. `snapshot_portfolio_report()` stores a denormalized snapshot in `portfolio_report_runs` and `portfolio_report_rows`.
7. `export_portfolio_report_workbook()` builds the Excel workbook.

### Production Data

Current sources:

- FusionSolar API day/month KPI calls:
  - `fetch_fusionsolar_kpi_day_rows()`
  - `fetch_fusionsolar_kpi_day_map()`
  - `fetch_fusionsolar_kpi_month_map()`
- Local persisted records:
  - `production_records`
  - `production_hourly_records`
- Sync/backfill:
  - `run_fusionsolar_production_sync()`
  - `run_fusionsolar_production_backfill()`
  - `upsert_production_record()`
  - `store_production_kpi_record()`

Current rules:

- Monthly individual reports prefer `period_type = 'month'` and fall back to daily records.
- Generic production reports use monthly records when present and fall back to daily records.
- Production value selection is centralized in `select_production_value()` / `select_production_kwh()`, but customer report normalization also reads provider payload keys directly.

### Consumption, Self-Consumption And Export

Current extraction:

- `normalize_customer_kpi_row()` reads:
  - production: `PVYield`, `inverterYield`, `inverter_power`
  - export: `ongrid_power`, `total_feed_in_to_grid`
  - self-use: `selfUsePower`, `selfProvide`
  - consumption: `use_power`, `day_use_energy`
- If self-use is missing and production exists, it infers `production - export`.
- If export is missing and production and self-use exist, it infers `production - self_use`.
- `normalize_customer_production_record()` may default all production to self-use when persisted payload lacks split data.

Risk:

- Monthly production is sometimes used as a substitute for energy-flow fields. Future tariff work must not infer hourly self-use from hourly production.

### Financial Calculation

Current location:

- `prepare_customer_report()` in `monitoring_board/customer_reports.py`.

Current formula:

- `savings_eur = self_use_kwh * electricity_price`
- `export_revenue_eur = export_kwh * sell_price`
- `total_benefit_eur = savings_eur + export_revenue_eur`
- ESCO payment: `production_kwh * solcor_price_per_kwh`
- EPC payment: `0`
- `net_benefit_eur = total_benefit_eur - solcor_payment_eur`
- Percentages:
  - autoconsumption: `self_use / production * 100`
  - export: `export / production * 100`
  - self-sufficiency: `self_use / consumption * 100`

Risk:

- Monetary values currently use `float`.
- ESCO billing base is hardcoded to total production.
- Manual form values are transient and not represented as persisted billing configuration.

### WAT

Current locations:

- Calculation:
  - `is_inverter_available()`
  - `inverter_availability_slot()`
  - `apply_inverter_edge_tolerance()`
  - `calculate_inverter_daily_availability()`
  - `calculate_weighted_plant_availability()`
- Persistence/sync:
  - `sync_fusionsolar_inverter_availability_for_date()`
  - `sync_fusionsolar_inverter_availability_range()`
  - `run_fusionsolar_inverter_availability_backfill()`
  - `recalculate_stored_inverter_availability()`
- Reports:
  - `get_inverter_availability_report()`
  - `get_monthly_wat_report_data()`
  - `get_daily_wat_report_data()`
  - `get_inverter_availability_chart_report()`

Current rule shape:

- An inverter is available when `active_power_kw > 0`.
- Valid plant slots are built from slots where at least one inverter has `active_power_kw > 0`.
- Edge tolerance removes early and late slots from each day.
- Inverter availability is `available_slots / valid_slots`.
- Plant availability is weighted by inverter nominal power if all included inverters have valid power; otherwise it falls back to a simple average.

Risks:

- Fallback to simple average can hide missing nominal power.
- Portfolio-level WAT is currently not a dedicated pure function weighted by plant installed power.
- Missing communication is represented through absent available slots during valid periods, but the behavior should be characterized explicitly before changing it.

### Helioscope And Degradation

Current locations:

- `parse_helioscope_monthly_expected()`
- `import_helioscope_file()`
- `calculate_degradation_factor()`
- `build_portfolio_report_rows()`

Current degradation formula:

- Months since mounting: `(report_year - mounting_year) * 12 + report_month - mounting_month`
- Years since mounting: `max(0, months) / 12`
- Factor: `max(0, 1 - 0.025 - years_since_mounting * 0.0055)`

Risk:

- This applies the 0.55% yearly degradation immediately from month 0 instead of only after the first 12 months.

### Tariffs And Invoices

Current tables:

- `asset_tariffs`
- `tariff_period_rules`
- `source_files` with `file_type = 'invoice'`
- `production_hourly_records`

Current functions:

- `classify_tariff_period()`
- `time_in_rule()`
- `calculate_tariff_value()`
- `get_latest_tariff()`
- `has_expired_tariff()`

Current behavior:

- Simple tariffs use monthly production directly.
- Multi-period tariffs classify hourly `production_hourly_records.production_kwh`.
- Periods crossing midnight are supported by `time_in_rule()`.
- Invoice association is only `asset_tariffs.invoice_file_id`.

Risks:

- Current hourly table has only `production_kwh`, not self-use/export/consumption/grid import.
- `calculate_tariff_value()` currently values production by tariff period; future avoided-grid-value logic must use hourly self-consumption.
- Invoice uploads only store files; no invoice validation or structured extraction exists.

### Snapshots

Current tables:

- `portfolio_report_runs`
- `portfolio_report_rows`

Current function:

- `snapshot_portfolio_report()`

Current behavior:

- Snapshots store denormalized portfolio report rows for one monthly report.
- They do not currently store selected columns, period type, source data versions, tariff resolution details, billing assumptions or full report configuration.

## Template Audit

Current reporting templates:

- `templates/exports.html`
- `templates/portfolios.html`
- `templates/portfolio_reports.html`
- `templates/performance.html`
- `templates/_performance_bar.html`
- `templates/dashboard.html`

Current template responsibilities:

- Render forms, filters, tables, KPIs and links.
- Format already-computed numbers with `format_number()` or Jinja `format`.
- Show conditional text such as `Sem dados`, `-`, selected tabs, selected filters and chip classes.
- Build URLs and pass user selections back to routes.

Calculations found in templates:

- `templates/portfolios.html` formats `mapping_confidence * 100` for display.
- `templates/_performance_bar.html` calls helper functions injected by Flask context, such as `compute_performance_percentage()`, `performance_bar_width()` and `reference_diagnostic()`.
- Other reviewed templates mostly format values that are already present in route/service output.

Risk:

- The portfolio and performance templates do not currently own the core reporting formulas, but they still depend on helper functions and one small display multiplication. Future reporting work should move all report-specific display derivations into prepared DTO fields where practical.
- Portfolio HTML tables and Excel exports duplicate column order and labels. This should move to `reporting/columns.py`.
- `templates/exports.html` includes explanatory formula text for savings/revenue. That text must stay synchronized with the canonical billing rules when billing modes are introduced.

## Related SQLite Tables

Existing reporting-related tables:

- `assets`
- `asset_integrations`
- `production_records`
- `production_hourly_records`
- `inverter_power_samples`
- `inverter_availability_daily`
- `plant_availability_daily`
- `portfolio_groups`
- `portfolio_assets`
- `source_files`
- `helioscope_expected_production`
- `asset_tariffs`
- `tariff_period_rules`
- `portfolio_report_runs`
- `portfolio_report_rows`
- `export_templates`

Existing indexes relevant to reporting:

- `idx_production_records_provider_period_asset`
- `idx_inverter_power_samples_asset_time`
- `idx_inverter_availability_daily_asset_date`
- `idx_plant_availability_daily_date_asset`
- `idx_portfolio_assets_portfolio_active`
- `idx_helioscope_expected_asset_month`
- `idx_asset_tariffs_asset_validity`
- `idx_production_hourly_asset_period`
- `idx_portfolio_report_runs_portfolio_month`

## Queries Currently Inside Routes

These should move behind repository/service functions over time:

- `exports()`:
  - report asset selection is delegated to `get_fusionsolar_report_assets()`, but route still parses financial inputs and orchestrates report generation/PDF response.
- `portfolios()`:
  - portfolio group listing.
  - portfolio config asset query with latest Helioscope, invoice and tariff subqueries.
  - asset list for manual mapping.
  - invoice list.
  - tariff rules list.
  - inserts/updates for portfolio assets and tariffs.
- `export_portfolio_mapping()`:
  - mapping export query.
  - workbook construction.
- `portfolio_reports()`:
  - groups query.
  - report runs query.
- `generate_portfolio_report()`:
  - snapshot orchestration and commit handling.
- `export_portfolio_report()`:
  - portfolio lookup.
  - workbook construction and file response.

## Duplicated Or At-Risk Formulas

The following formulas should have one canonical home:

- Month bounds:
  - `normalize_report_month()`
  - `report_period_dates()`
  - `portfolio_reports.month_bounds()`
  - ad hoc month start/end in customer report builders.
- Production/self-use/export inference:
  - `normalize_customer_kpi_row()`
  - `normalize_customer_production_record()`
  - customer report fallback logic.
- Financial benefit:
  - `prepare_customer_report()`
  - future individual, portfolio, PDF and Excel outputs need to consume the same DTO.
- Degradation:
  - `calculate_degradation_factor()` currently lives in portfolio presentation/orchestration code.
- Tariff period matching:
  - `time_in_rule()` and `classify_tariff_period()` should be reused by all tariff consumers.
- Tariff value:
  - `calculate_tariff_value()` should not be duplicated for HTML, Excel and PDF.
- WAT:
  - inverter daily availability, plant weighted availability and future portfolio weighted availability should be separate pure functions.
- Portfolio totals:
  - `aggregate_portfolio_total()` currently defines totals for HTML and Excel indirectly. Future PDF must use the same column definitions and total rules.
- Formatting/columns:
  - `templates/portfolios.html`, `templates/portfolio_reports.html`, and `export_portfolio_report_workbook()` define parallel report shapes.

## Proposed Package Architecture

Target package:

```text
monitoring_board/reporting/
|-- __init__.py
|-- models.py
|-- periods.py
|-- billing.py
|-- tariffs.py
|-- availability.py
|-- degradation.py
|-- repositories.py
|-- services.py
|-- columns.py
|-- snapshots.py
|-- pdf.py
`-- excel.py
```

### `models.py`

Responsibilities:

- Enums and DTOs only.
- No SQLite, Flask, ReportLab or OpenPyXL imports.

Initial types:

- `ReportPeriodType`
- `BillingMode`
- `BillingEnergyBase`
- `ReportType`
- `TariffType`
- `ReportingPeriod`
- `EnergyBreakdown`
- `BillingConfig`
- `BillingResult`
- `TariffConfig`
- `TariffPeriodRule`
- `HourlyEnergyRecord`
- `AvailabilityResult`
- `PortfolioReportRow`
- `PortfolioReportTotal`
- `ReportWarning`

Move:

- report type strings from `customer_reports.REPORT_TYPES` should become `ReportType` for calculations, while display config can stay in `customer_reports.py` until PDF refactor.

### `periods.py`

Responsibilities:

- Build date ranges and labels for monthly, quarterly, semiannual and annual reports.
- Return included months and month count.
- Clip current periods to today where required.

Move:

- `normalize_report_month()`
- `normalize_report_year()`
- `report_period_dates()`
- `report_period_label()`
- `portfolio_reports.month_bounds()`
- repeated month start/end logic in customer report builders.

Keep temporarily:

- Compatibility wrappers in `app_factory.py` until routes are migrated.

### `billing.py`

Responsibilities:

- Pure financial calculations using `Decimal`.
- ESCO/EPC rules.
- Energy billing versus fixed monthly fee.
- Self-consumption versus total-production billing base.
- Grid purchase calculation.

Move:

- Financial formulas from `prepare_customer_report()`.

Do not include:

- SQL.
- Flask request parsing.
- PDF labels/layout.

### `tariffs.py`

Responsibilities:

- Tariff period classification.
- Simple/bi/tri/tetra tariff value calculations.
- Midnight-crossing periods.
- Validation of incomplete hourly energy.
- Avoided-grid-value calculation based on hourly self-consumption, not production.

Move:

- `time_in_rule()`
- `classify_tariff_period()`
- `calculate_tariff_value()`

Keep temporarily:

- `get_latest_tariff()` should move to `repositories.py`, not `tariffs.py`.

### `availability.py`

Responsibilities:

- Pure WAT rules.
- Inverter availability.
- Plant availability weighted by inverter nominal power.
- Portfolio WAT weighted by plant installed nominal power.
- Warnings for missing nominal power.

Move:

- `is_inverter_available()`
- `inverter_availability_slot()`
- `apply_inverter_edge_tolerance()`
- `calculate_inverter_daily_availability()`
- `calculate_weighted_plant_availability()`

Keep temporarily:

- FusionSolar fetch/sync functions stay in `app_factory.py` or a provider integration module until service extraction.

### `degradation.py`

Responsibilities:

- Helioscope degradation factor.
- Date-safe implementation using `date`.

Move:

- `calculate_degradation_factor()`.

Future correction:

- Apply 2.5% for first 12 months and 0.55% per year only after month 12.

### `repositories.py`

Responsibilities:

- SQLite reads/writes for reporting only.
- No business formulas.
- Accept explicit `sqlite3.Connection`.
- Return rows/DTOs ready for services.

Move:

- Portfolio group and asset queries.
- Latest source file queries.
- Latest/valid tariff queries.
- Tariff rule queries.
- Production record queries.
- Hourly energy queries.
- Availability queries.
- Helioscope expected production queries.
- Snapshot persistence.
- Billing config queries when introduced.

Should not do:

- Build PDFs or Excel files.
- Parse Flask requests.
- Decide business warnings beyond data presence flags.

### `services.py`

Responsibilities:

- Orchestrate individual and portfolio reports.
- Combine repositories and pure rule modules.
- Produce DTOs/dicts consumed by HTML/PDF/Excel.
- Own warnings and missing-data decisions.

Move:

- `build_local_customer_production_report()`
- provider-independent parts of `build_fusionsolar_customer_production_report()`
- `build_portfolio_report_rows()`
- `aggregate_portfolio_total()` orchestration
- `build_portfolio_kpis()`
- `filter_report_rows()`

Provider-specific API fetches should stay outside reporting services or be passed in through sync/population layers.

### `columns.py`

Responsibilities:

- Central portfolio column definitions.
- Presets:
  - executive summary
  - production/performance
  - financial
  - complete
  - custom
- Formatting metadata.
- Total behavior metadata.
- PDF width hints.

Move:

- Portfolio table header definitions from templates and Excel.

### `snapshots.py`

Responsibilities:

- Snapshot serialization/deserialization.
- Future versioning of report snapshots.
- Store report configuration, period, columns, warnings and source assumptions.

Can be merged into `repositories.py` initially if this remains small.

### `pdf.py` And `excel.py`

Responsibilities:

- Presentation only.
- Consume service output and column definitions.
- No formulas except formatting.

Migration:

- Keep `customer_reports.py` and `portfolio_reports.py` initially as public facades.
- Move implementation behind those facades gradually.

## Dependency Rules

Allowed dependencies:

- `models.py`: stdlib only.
- `periods.py`: `models.py`.
- `billing.py`: `models.py`, `decimal`, `datetime`.
- `tariffs.py`: `models.py`, `periods.py` if needed.
- `availability.py`: `models.py`.
- `degradation.py`: `datetime`, optionally `decimal`.
- `repositories.py`: `sqlite3`, `models.py`, `periods.py`, `monitoring_board.db`.
- `services.py`: pure modules plus `repositories.py`.
- `columns.py`: `models.py`.
- `pdf.py`: service DTOs, `columns.py`, ReportLab.
- `excel.py`: service DTOs, `columns.py`, OpenPyXL.

Forbidden dependencies:

- No reporting domain module imports Flask.
- No pure formula module imports SQLite.
- No template/PDF/Excel module computes business formulas.
- No repository module calls external APIs.
- No route computes financial values, periods, degradation, availability, tariffs or portfolio aggregation.

## Functions To Move

High-priority pure functions:

- `customer_reports.detect_report_type()`
- financial section of `customer_reports.prepare_customer_report()`
- `portfolio_reports.parse_float()` or replace with typed Decimal parsing where money is involved.
- `portfolio_reports.calculate_degradation_factor()`
- `portfolio_reports.time_in_rule()`
- `portfolio_reports.classify_tariff_period()`
- `portfolio_reports.calculate_tariff_value()`
- `portfolio_reports.month_bounds()`
- `portfolio_reports.aggregate_portfolio_total()`
- WAT pure functions from `app_factory.py`.

Repository candidates:

- `get_fusionsolar_report_assets()`
- `get_latest_tariff()`
- `has_expired_tariff()`
- `get_monthly_availability()`
- production record queries in `build_local_customer_production_report()`
- portfolio config query in `portfolios()`
- portfolio mapping export query.
- `snapshot_portfolio_report()` write logic.

Service candidates:

- `build_local_customer_production_report()`
- provider-independent part of `build_fusionsolar_customer_production_report()`
- `build_portfolio_report_rows()`
- `build_portfolio_kpis()`
- `filter_report_rows()`
- WAT report assembly functions.

Presentation candidates:

- `export_portfolio_report_workbook()`
- `export_rows_file()` if generic exports are later unified.
- PDF drawing functions from `customer_reports.py` can remain there until the PDF period refactor.

## Functions To Keep Temporarily

- Routes in `app_factory.py` should remain as HTTP adapters until services are ready.
- FusionSolar API session and fetch functions should stay in integration code until reporting no longer needs live API fallback.
- `customer_reports.REPORT_TYPES` can stay as PDF display configuration.
- `customer_reports.build_customer_report_pdf()` can stay for Phase 1-6 to preserve visual output.
- `portfolio_reports.PORTFOLIO_EXTERNAL_ROWS` can stay until mapping is reworked in Phase 9.

## Incremental Migration Strategy

1. Add characterization tests before each extraction.
2. Introduce new pure module function.
3. Keep old public function name as wrapper if tests/routes import it.
4. Run focused and full test suites.
5. Move route queries to repositories only after service output is covered.
6. Move templates/PDF/Excel to service DTOs only after column definitions are centralized.
7. Add schema columns/tables only through idempotent `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS` and `ensure_column()`.
8. Never remove existing tables/columns during the migration phases.

## Testing Strategy

Characterization tests before refactor:

- ESCO individual monthly report.
- EPC individual monthly report.
- Local-data customer report path.
- API-fallback customer report path with mocked FusionSolar.
- Portfolio row with missing production, Helioscope, availability, tariff and invoice.
- Portfolio totals.
- WAT daily calculation and reporting.
- Helioscope parser and degradation factor.
- Portfolio Excel headers and totals.

Unit tests for pure modules:

- Period boundaries including leap years and current periods.
- Billing with `Decimal`, zero prices, fixed monthly fee and different billing bases.
- Tariff classification at boundaries and midnight-crossing intervals.
- Incomplete hourly energy warnings.
- WAT valid slots, missing communication during valid slots, and missing nominal power warnings.
- Degradation factor boundary cases.
- Portfolio total rules.

Integration tests with temporary SQLite:

- Schema compatibility for each new table/column.
- Billing config persistence.
- Tariff validity and invoice resolution.
- Portfolio report build from stored production/availability/tariff/Helioscope data.
- Snapshot persistence and replay.

Regression tests:

- Existing `tests/test_customer_reports.py`.
- Existing `tests/test_portfolio_reports.py`.
- Existing `tests/test_performance.py`.
- Existing `tests/test_inverter_time_availability.py`.
- Generic export tests where relevant.

## Future Database Shape

Additions should be incremental and idempotent.

Recommended future tables:

### `asset_billing_configs`

Purpose: persistent billing configuration per installation.

Fields:

- `id`
- `asset_id`
- `billing_mode`
- `solcor_price_per_kwh`
- `fixed_monthly_fee_eur`
- `billing_energy_base`
- `electricity_price_source`
- `default_electricity_price`
- `default_export_price`
- `valid_from`
- `valid_to`
- `created_at`
- `updated_at`
- `notes`

### Expanded `production_hourly_records`

Current fields:

- `production_kwh`

Future nullable fields:

- `self_use_kwh`
- `export_kwh`
- `consumption_kwh`
- `grid_import_kwh`

Rule:

- Missing external data stays `NULL`; do not silently replace with zero.

### Invoice History

Either extend `source_files` or add `asset_invoices`.

Recommended `asset_invoices`:

- `id`
- `asset_id`
- `source_file_id`
- `original_filename`
- `uploaded_at`
- `valid_from`
- `valid_to`
- `active`
- `tariff_id`
- `extracted_json`
- `validation_status`
- `extraction_warnings_json`
- `created_at`
- `updated_at`

### Tariff Templates

Recommended:

- `tariff_templates`
- `tariff_template_rules`

Purpose:

- Editable BTN/MT daily/weekly cycles and custom templates.

### Report Snapshot Metadata

Extend or add:

- `report_type`
- `period_type`
- `period_start`
- `period_end`
- `period_label`
- `columns_json`
- `config_json`
- `warnings_json`
- `source_version_json`

Existing `portfolio_report_runs.report_month` should remain for compatibility.

## Recommended Phase Order

1. Phase 1: characterization tests and foundations.
2. Phase 2: WAT and degradation corrections.
3. Phase 3: billing configuration and ESCO logic.
4. Phase 4: generic reporting periods.
5. Phase 5: hourly energy and tariffs.
6. Phase 6: invoice tariff history and assisted extraction.
7. Phase 7: generic individual PDF.
8. Phase 8: configurable portfolio reports.
9. Phase 9: portfolio mapping correction.
10. Phase 10: integration hardening and cleanup.

## Concrete Reduction Points For `app_factory.py`

Move first:

- Period parsing helpers to `reporting/periods.py`.
- Individual report data assembly to `reporting/services.py`.
- Portfolio config queries to `reporting/repositories.py`.
- Portfolio report route orchestration to `reporting/services.py`.
- WAT pure calculations to `reporting/availability.py`.

Move later:

- Production report row building to reporting services/repositories.
- Portfolio exports to `reporting/excel.py`.
- Snapshot persistence to `reporting/repositories.py` or `reporting/snapshots.py`.
- Customer PDF response should remain a route concern, but PDF byte generation should stay outside `app_factory.py`.

Routes should eventually do only:

1. Parse and validate HTTP input.
2. Call service/repository functions.
3. Flash/redirect/render/send file.

## Known Open Risks

- FusionSolar energy field semantics need verification before tariff and billing expansion.
- Current WAT fallback to simple average can hide missing inverter power.
- Current degradation formula does not match the target requirement after month 12.
- Current `portfolio_assets` uniqueness on `(portfolio_id, asset_id)` may be too restrictive for repeated subaccounts or multiple installations from one company.
- Portfolio report columns are duplicated between HTML and Excel.
- `production_hourly_records` cannot currently support avoided grid value because self-use is not stored.
- PDF visual layout is monthly-only and assumes daily bars.
- Existing snapshots do not capture enough configuration to reproduce reports after tariff/billing changes.
