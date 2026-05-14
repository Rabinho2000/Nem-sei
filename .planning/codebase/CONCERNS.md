# Codebase Concerns

## Highest-Risk Areas

- `app.py` is the main operational risk. It is about 10,601 lines and combines Flask routes, schema management, imports, reports, integration clients, alerting, scheduling, and background job execution in one module. Small changes can have wide side effects because many helpers share globals, `g.db`, and module-level constants.
- `monitoring_board/routes/field_routes.py` is another large module at about 1,476 lines. It mixes schema migration, route UI, geocoding, route optimization, external API access, CSV export, and KML parsing. It is newer and has tests, but the feature has many synchronous network/database paths.
- SQLite is used for all persistence while APScheduler, threaded Gunicorn, request handlers, and background jobs can write concurrently. `monitoring_board/db.py` sets WAL and a 10s busy timeout, but long sync/backfill/report jobs still share the same database file.
- External API assumptions are central to correctness. FusionSolar, Sigenergy, Telegram, OpenRouteService, and Google My Maps response shapes, rate limits, timestamps, and units are inferred in local code and should be treated as fragile.

## Architecture / Maintainability

- `app.py` starts with `create_app()` at `app.py:289`, then embeds most route handlers and domain functions through `app.py:10601`. The separation into `monitoring_board/services/fusionsolar.py` is small compared with the FusionSolar logic still in `app.py`.
- Many concerns have duplicate or overlapping implementations: FusionSolar status/name helpers exist in `app.py` and `monitoring_board/services/fusionsolar.py`; route calculations have both `build_route_segments()` and older-looking `optimize_route()` / `fetch_openrouteservice_route()` paths in `monitoring_board/routes/field_routes.py`.
- Runtime schema changes are distributed. Core schema is created by `ensure_database()` in `app.py:2744`; field route schema is created by `ensure_field_routes_schema()` in `monitoring_board/routes/field_routes.py:119` and is also called from request handlers. This makes migration ordering and failure recovery harder to reason about.
- The schema migration style uses repeated `ensure_column()` calls (`app.py:3052` onward and `monitoring_board/routes/field_routes.py:122` onward). This is pragmatic but lacks migration versions, rollback, and explicit data backfills.
- `ensure_column()` in `monitoring_board/db.py:34` builds `PRAGMA table_info(...)` and `ALTER TABLE ... ADD COLUMN ...` using string interpolation. Current callers use internal constants, but the helper is unsafe if future code passes user-controlled table names or definitions.
- Several functions commit internally instead of leaving transaction scope to callers, for example `rebuild_asset_alias_blob()` at `app.py:4673`, `import_excel_data()` at `app.py:4679`, `import_daily_monitoring()` at `app.py:4973`, and many background job functions. Nested commit behavior makes larger workflows harder to make atomic.

## SQLite / Concurrency

- Gunicorn runs with one worker and four threads (`Dockerfile:17`, `docker-compose.yml:14`). APScheduler runs in-process via `start_integration_scheduler()` at `app.py:7023`, so the one-worker deployment constraint is important. Increasing workers would duplicate scheduled jobs.
- Background jobs are scheduled into APScheduler with `schedule_background_job()` at `app.py:7093` and run using the same process and SQLite file via `run_background_job()` at `app.py:7128`. A long job can compete with request writes and scheduled syncs.
- Background duplicate prevention in `create_background_job()` only checks `job_type` for any pending/running job. This prevents duplicate work, but also blocks legitimate independent jobs of the same type with different dates/assets.
- `mark_stale_running_background_jobs_failed()` is startup recovery only. If the scheduler thread dies while the app stays alive, pending jobs may sit unscheduled until restart or another scheduling path runs.
- `run_fusionsolar_production_backfill()` at `app.py:9416` can sleep between API calls, wait through cooldowns, and process many records inside one background job. Even with commits during waits, it is a heavy workload for the same Flask process serving users.
- The local repository currently contains `monitoring_board.db-wal` and `monitoring_board.db-shm` as untracked files. `.gitignore` excludes `*.db` but not SQLite WAL/SHM sidecars, so runtime database sidecars can accidentally appear in Git status.

## Scheduler / Background Work

- APScheduler is a module global (`SCHEDULER` at `app.py:283`). It is guarded only within one Python process. This is acceptable for the documented one-worker deployment but fragile under debug reloaders, multiple workers, or a second app instance.
- Scheduled sync uses `run_scheduled_integration_sync()` at `app.py:7080`, which currently calls `run_fusionsolar_sync()` for any provider value. Provider dispatch exists elsewhere, but this scheduled path should be verified for Sigenergy behavior before enabling Sigenergy auto-sync.
- `refresh_integration_scheduler()` removes jobs by ID prefix at `app.py:7033`. Manual or future scheduler jobs with matching prefixes could be removed unintentionally.
- `run_background_job()` marks jobs running and commits before executing, then records success/failure. This is reasonable, but there is no retry policy, progress heartbeat, cancellation, or separate worker isolation.

## Security / Secrets

- The app has good basics: forced `FLASK_SECRET_KEY` in `monitoring_board/security.py:16`, CSRF in `create_app()` at `app.py:316`, hardened session defaults in `app.py:296`, and safe contract path checks in `app.py:1370`.
- Plain `APP_PASSWORD` is still supported in `monitoring_board/security.py:29`. Docs prefer `APP_PASSWORD_HASH`, but production safety depends on operators following that recommendation.
- Integration credentials can be stored in SQLite through the integrations form (`app.py:2147`, password update at `app.py:2160`). Environment variables override those values, but database backups may still contain provider credentials.
- Telegram bot token is embedded in the request URL in `monitoring_board/services/telegram_service.py:67`. Failed request logging avoids the URL, but upstream/proxy logs outside the app could expose the token.
- Login has no rate limiting or lockout in `monitoring_board/routes/auth.py:21`. Cloudflare Access is recommended in docs, but the Flask app itself does not slow brute-force attempts.
- `SESSION_COOKIE_SECURE` defaults to false unless env overrides it (`app.py:299`). Docker Compose sets it true, but local/network deployments launched with `python app.py --host 0.0.0.0` need explicit care.
- `settings()` accepts a server-local Excel path from the authenticated UI (`app.py:2700`). This is fine for a trusted admin tool, but it means an authenticated user can make the app read arbitrary accessible `.xlsx/.xlsm` files.

## External Integrations

- FusionSolar sessions are cached in process globals (`FUSIONSOLAR_SESSION_CACHE` at `app.py:284`, `get_fusionsolar_session()` at `app.py:7226`). Cache state is lost on restart and not shared across processes.
- FusionSolar rate limit detection is string-based in `is_fusionsolar_rate_limit_error()` at `app.py:7597`; session expiry detection is also string-based. Changes in API messages or fail code formatting could bypass handling.
- FusionSolar production selection prefers hardcoded keys `PVYield`, `inverterYield`, and `inverter_power` in `select_production_value()` at `app.py:7568`. Unit assumptions should be validated against the Northbound API for each endpoint.
- Timestamp parsing uses local `datetime.fromtimestamp()` in `parse_fusionsolar_collect_date()` at `app.py:7411`. If FusionSolar timestamps are UTC or plant-local, date boundaries may be wrong around midnight/DST.
- Sigenergy support exists (`app.py:8476` onward) but appears less covered than FusionSolar. Scheduled dispatch and UI language still center FusionSolar, so Sigenergy should be treated as partially mature.
- OpenRouteService calls in `monitoring_board/routes/field_routes.py:842` and geocoding at `monitoring_board/routes/field_routes.py:1018` run in request handlers. Slow network/API failures directly delay the web request.
- Google My Maps KML import downloads a URL derived from user input (`monitoring_board/routes/field_routes.py:1061`). It restricts to Google KML form, but still depends on remote content size and XML parsing in the request path.

## Data Correctness

- Date/time usage is mostly naive `datetime.now()` / `date.today()` across monitoring, alerts, schedules, reports, and rate-limit state. APScheduler is set to `Europe/Lisbon`, but records usually do not store timezone offsets.
- The latest monitoring view orders by `record_date` plus row id in `ensure_database()` at `app.py:2744`. Same-day imports with multiple sources depend on insert order rather than source priority or imported timestamp.
- `import_daily_monitoring()` at `app.py:4973` can auto-resolve previous problem assets when `import_scope == "complete"`. This is useful but dangerous if the pasted source is incomplete or filtered.
- Excel import in `import_excel_data()` at `app.py:4679` relies on sheet names and fixed column indexes. Schema drift in the workbook can silently map wrong fields after basic sheet existence succeeds.
- `upsert_asset_from_excel()` at `app.py:4853` updates many fields on existing assets based on project name/alias matching. A bad alias or renamed project can overwrite operational metadata.
- Performance baselines and reports depend on local `kwp`, monthly budget JSON, historical production rows, and selected FusionSolar production keys. Missing or malformed local metadata can produce "Sem referencia" or misleading deviations.
- Route optimization uses nearest-neighbour heuristics for larger stop counts (`monitoring_board/routes/field_routes.py:793` and `:806`). It is practical, but not guaranteed optimal and should not be sold as exact optimization.

## Performance / Resource Use

- Dashboard/report queries are mostly direct SQL and can grow expensive as `monitoring_records`, `production_records`, and `payload_json` grow. Some indexes exist in `ensure_database_indexes()` at `app.py:3078`, but there is no retention/archive strategy.
- `production_records.payload_json`, `monitoring_import_batches.raw_input`, and integration unresolved payloads store raw external/import data. This is helpful for debugging but can grow the SQLite file quickly on a Raspberry Pi.
- PDF generation in `export_customer_production_pdf()` at `app.py:6316` and export building in `build_export_dataset()` at `app.py:5780` run synchronously in requests.
- Contract uploads check extension, `%PDF-` header, max request size, and path containment (`app.py:1269`), but uploaded PDFs are not virus-scanned and are stored on local disk.
- `create_database_backup()` in `monitoring_board/db.py:26` uses `shutil.copy2()` on the database path. The shell backup script uses SQLite `.backup`, but the in-app Excel import backup may be inconsistent with WAL activity.

## Testing Gaps / Fragile Coverage

- There are focused tests for security, DB helpers, FusionSolar sync/backfill, performance references, alerts, reports, and field routes. This is a strong base for a small app.
- Tests do not appear to exercise real scheduler behavior, multi-threaded SQLite contention, or multiple Gunicorn workers. These are deployment-critical assumptions.
- Integration tests use mocked API shapes. There is no contract test fixture suite pinned to real FusionSolar/Sigenergy API examples, so response-shape drift remains a major risk.
- PDF visual/layout correctness and Excel workbook schema drift are only partially covered by functional tests.
- Telegram alert spam controls are tested, but real Telegram failures, parse-mode edge cases, and rate limits are not deeply simulated.

## Operational Concerns

- Deployment intentionally keeps one worker because APScheduler lives inside the app (`README.md:113`). This should remain explicit in all deployment docs and process managers.
- Backups are documented and scripted, but restore is manual. There is no automated backup verification or health check endpoint.
- Logs and database live under `DATA_DIR`, which is good for Docker, but default local mode writes runtime state into the repository directory. This keeps local Git status noisy and increases accidental artifact risk.
- The app is admin-oriented and assumes trusted users. If access expands beyond the owner/admin, authorization is too coarse: all authenticated users can mutate assets, credentials, settings, imports, and reports.

## Recommended Watch List

- Keep any new work small and covered by tests when it touches `app.py`, background jobs, SQLite writes, alerts, report calculations, imports, or external API parsing.
- Prioritize extracting cohesive modules only when changing nearby behavior: FusionSolar client, performance calculations, Excel import, report generation, and scheduler/job orchestration are the clearest candidates.
- Add migration versioning before more schema changes accumulate.
- Add `.gitignore` entries for `*.db-wal` and `*.db-shm`.
- Prefer storing provider secrets only in environment variables for production; treat DB backups as sensitive.
- Verify FusionSolar units, timestamp timezone, pagination, and fail codes against current API examples before relying on new production/report calculations.
