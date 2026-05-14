# Architecture Map

## System Shape

This repository is a server-rendered Flask monitoring board for PV O&M operations. It is organized as a mostly monolithic Flask application in `app.py`, with a small extracted package under `monitoring_board/` for runtime paths, SQLite helpers, security, logging, blueprints, and reusable service helpers.

The dominant pattern is:

- Flask app factory and most route handlers in `app.py`.
- SQLite as the application database, initialized and migrated in application code.
- Jinja templates in `templates/` render all primary screens.
- Static CSS and image assets live in `static/`.
- APScheduler runs in-process for scheduled integration syncs, Telegram summaries, and queued background jobs.
- External services are called directly with `requests`.

The deployment architecture is intentionally simple: `docker-compose.yml` runs one Gunicorn worker with four threads and mounts persistent runtime data into `/data`. The single worker matters because APScheduler is started inside the Flask process.

## Entry Points

- `app.py` is the main application module and WSGI entry point. It creates the global `app = create_app()` near the end of the file.
- `create_app()` in `app.py` builds the Flask app, configures runtime paths, registers blueprints, initializes SQLite, seeds defaults, and starts APScheduler.
- Running `python app.py` uses `parse_cli_args()` and `app.run(...)` for local development.
- Docker/Gunicorn uses `app:app` through the command in `docker-compose.yml`.
- `monitoring_board/routes/auth.py` registers `/login` and `/logout`.
- `monitoring_board/routes/field_routes.py` registers the `/field-routes` blueprint and its route-planning endpoints.

## Request Lifecycle

`create_app()` configures global request behavior:

- `before_request` opens `g.db` using `monitoring_board.db.get_db()`.
- All `POST` requests must pass CSRF validation using `monitoring_board.security.csrf_token()`.
- All non-login, non-static requests require `session["authenticated"]`.
- `teardown_request` logs request duration and closes `g.db`.
- `context_processor` injects shared template globals such as statuses, formatting helpers, current username, and CSRF token.
- Error handlers render `templates/error.html` for 400, 404, 413, and 500 responses.

The main route handlers in `app.py` combine HTTP parsing, SQL queries, business rules, persistence, flash messages, and template rendering. There is not a strict controller/service/repository split yet.

## Layers And Responsibilities

### Application Layer

`app.py` is the central orchestration layer. It contains:

- Dashboard, assets, monitoring, tickets, exports, integrations, Telegram alerts, renewals, settings, and performance routes.
- SQLite schema creation and lightweight migrations.
- Import/export workflows for Excel, CSV/XLSX, and PDF reports.
- Integration sync orchestration for FusionSolar and Sigenergy.
- Telegram alert policy, throttling, and message generation.
- Performance calculations, production backfill, expected production, and reference recalculation.
- Background job persistence and APScheduler execution.

Because this file is over 10,000 lines, feature boundaries are mostly indicated by function groups and route paths rather than package/module boundaries.

### Package Helpers

`monitoring_board/db.py` owns low-level SQLite connection setup and helper utilities:

- `get_db()` configures row factory, foreign keys, busy timeout, and conservative PRAGMAs.
- `configure_database_for_runtime()` enables WAL mode.
- `ensure_column()` supports code-driven migrations.
- `query_all()` and `query_scalar()` wrap common query patterns.
- `create_database_backup()` copies the database before risky imports.

`monitoring_board/runtime.py` owns runtime path resolution:

- Default local runtime paths point at the repo root.
- `DATA_DIR` redirects database, uploads, backups, contracts, and logs to a persistent data directory.
- File path helpers convert between stored runtime-relative paths and resolved filesystem paths.

`monitoring_board/security.py` owns app login secrets and CSRF token creation:

- `FLASK_SECRET_KEY` is required and insecure defaults are rejected.
- Login uses `APP_USERNAME` and either `APP_PASSWORD_HASH` or `APP_PASSWORD`.
- CSRF tokens are stored in the Flask session.

`monitoring_board/logging_config.py` configures rotating file logging.

`monitoring_board/services/fusionsolar.py` contains reusable normalization helpers for provider URLs, sync hours, and FusionSolar status mapping. Most FusionSolar API orchestration still lives in `app.py`.

`monitoring_board/services/telegram_service.py` contains environment-driven Telegram configuration and the low-level `sendMessage` call. Alert decisions and message composition live in `app.py`.

### Blueprints

`monitoring_board/routes/auth.py` is a small, self-contained auth blueprint.

`monitoring_board/routes/field_routes.py` is a larger feature blueprint for field route planning. It owns:

- Field route schema initialization via `record_once`.
- Route-plan CRUD and CSV export.
- Asset coordinate updates and confirmation.
- Missing-coordinate geocoding through OpenRouteService.
- My Maps KML import.
- Route optimization, segment construction, and cost calculation.

The blueprint still uses direct SQLite queries and Flask globals, matching the style of `app.py`.

## Data Model

SQLite is the system of record. The core schema is created in `ensure_database()` in `app.py` and extended by `ensure_field_routes_schema()` in `monitoring_board/routes/field_routes.py`.

Primary application tables:

- `assets`: PV installations and contract/contact metadata.
- `asset_aliases`: alternate names used for matching imports and integrations.
- `monitoring_records`: daily operational status history.
- `monitoring_unmatched`: pasted/imported monitoring rows that could not be matched.
- `monitoring_import_batches`: import/sync batch metadata.
- `tickets` and `ticket_visits`: corrective tickets and visit history.
- `om_contracts`: O&M contract details and uploaded PDF path metadata.
- `export_templates`: predefined and custom export definitions.
- `integration_configs`: provider configuration for FusionSolar and Sigenergy.
- `asset_integrations`: mapping between local assets and provider plant/system identifiers.
- `integration_sync_runs`: sync audit trail.
- `integration_unresolved`: provider rows awaiting manual resolution.
- `production_records`: daily/monthly production and performance records.
- `performance_settings`: asset-specific thresholds and baseline settings.
- `telegram_alerts`, `alert_settings`, `alert_blacklist`, `alert_baseline`: Telegram alert policy and audit data.
- `background_jobs`: persisted queue for long-running performance jobs.
- `app_state`: general key/value operational state.
- `field_route_plans`, `field_route_stops`, `field_route_segments`: route planning data owned by the field routes blueprint.

`latest_monitoring_view` is a SQLite view used to find the latest monitoring row per asset.

The application uses incremental `ensure_column()` calls for schema drift instead of a migration framework.

## Data Flow

### Manual UI Flow

1. User logs in through `monitoring_board/routes/auth.py`.
2. Authenticated requests open `g.db` per request.
3. Route handlers read form/query data, run direct SQL, apply local business rules, commit on writes, and render Jinja templates.
4. Templates under `templates/` extend `templates/base.html` and use shared globals injected by `create_app()`.

### Excel Import Flow

1. Settings accepts an Excel path or startup auto-import finds the first root `.xlsx`.
2. `create_database_backup()` backs up SQLite before reimport from the settings page.
3. `import_excel_data()` reads workbook sheets with `openpyxl`.
4. `upsert_asset_from_excel()` and related import helpers write assets, monitoring rows, aliases, contracts, and tickets.

### Monitoring Import Flow

1. `/monitoring` accepts pasted table data.
2. `parse_monitoring_lines()` normalizes input.
3. `import_daily_monitoring()` matches rows to assets through names and aliases.
4. Matched rows become `monitoring_records`; unmatched rows become `monitoring_unmatched`.
5. Alert event construction and Telegram notification logic can run from new monitoring events.

### Integration Sync Flow

1. `/integrations` or APScheduler triggers `run_all_integration_syncs()` / `run_fusionsolar_sync()`.
2. Provider-specific API functions fetch stations, realtime KPI, alarms, production KPI, or Sigenergy system data.
3. Rows are normalized to local statuses and matched to assets through `asset_integrations`, exact names, aliases, or suggested matches.
4. Matched provider rows create monitoring records and update integration mappings.
5. Unmatched provider rows are stored in `integration_unresolved`.
6. Sync history is recorded in `integration_sync_runs`.
7. Alert events are processed and optionally sent through Telegram.

### Performance And Background Jobs

1. Performance routes enqueue rows in `background_jobs` for production sync, backfill, monthly cycle, or reference recalculation.
2. `schedule_background_job()` registers a one-shot APScheduler job.
3. `run_background_job()` marks the job running, dispatches through `run_background_job_payload()`, then stores result or error JSON.
4. Production data is stored in `production_records`.
5. Baseline and expected production calculations use historical production records, asset kWp, and `performance_settings`.

### Report And Export Flow

Exports are handled in `app.py`:

- `build_export_dataset()` selects the dataset and rows.
- Report builders construct monitoring, executive, and production report rows.
- `export_rows_file()` emits CSV/XLSX data.
- `export_customer_production_pdf()` uses ReportLab to generate customer-facing PDF reports.

## Background Execution

APScheduler is global process state in `SCHEDULER`.

- `start_integration_scheduler()` starts the scheduler once per process.
- `refresh_integration_scheduler()` removes and recreates integration and daily summary jobs from DB/env config.
- `schedule_pending_background_jobs()` recovers pending persisted jobs on startup and marks stale running jobs as failed.
- Long-running work uses SQLite job rows plus APScheduler execution, not a separate worker service.

Concurrency control is mostly lightweight:

- SQLite connections use WAL mode, busy timeout, and per-request/per-job connections.
- FusionSolar session and sync operations use process-local locks.
- Docker intentionally runs one Gunicorn worker to avoid duplicate schedulers.

## External Boundaries

- FusionSolar: API sessions, XSRF token extraction, station/realtime/alarm/KPI endpoints, rate-limit handling, production sync/backfill.
- Sigenergy: token auth and system/realtime/energy-flow endpoints.
- Telegram Bot API: message sending and connection testing.
- OpenRouteService: geocoding and route directions for field route planning.
- Local filesystem: SQLite DB, uploads/contracts, backups, reports/logs, Excel import files.
- Docker/Cloudflare deployment: documented in `docs/raspberry-pi-deployment.md`.

## Architectural Constraints

- Keep the single-process scheduler assumption intact unless scheduling is redesigned.
- Be careful with long SQLite transactions during imports, syncs, reports, and background jobs.
- New shared behavior should usually be extracted from `app.py` only when it reduces risk or duplication for a focused change.
- Schema changes currently belong in `ensure_database()` or `ensure_field_routes_schema()` with `ensure_column()` compatibility.
- Runtime artifacts such as `.env`, `monitoring_board.db`, WAL/SHM files, uploads, backups, logs, reports, PDFs, and Excel files are local data, not application code.
