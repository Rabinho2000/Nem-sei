# Technology Stack

## Runtime

- Primary language: Python, using modern type hints and `from __future__ import annotations`; main entrypoint is `app.py`.
- Target runtime: Python 3.12 in Docker via `FROM python:3.12-slim` in `Dockerfile`.
- Local development starts the Flask dev server with `python app.py`; production/container runs Gunicorn with `gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app` from `Dockerfile` and `docker-compose.yml`.
- Deployment target is Raspberry Pi 5 / Raspberry Pi OS 64-bit, documented in `docs/raspberry-pi-deployment.md`.

## Web Framework

- Flask is the web framework (`flask>=3.1.3` in `requirements.txt`).
- The main app factory is `create_app()` in `app.py`; it configures secret key, sessions, upload limits, database path, blueprints, and request lifecycle hooks.
- Main routes are currently concentrated in `app.py`, with additional blueprints in `monitoring_board/routes/auth.py` and `monitoring_board/routes/field_routes.py`.
- Templates are server-rendered Jinja files in `templates/`; shared layout is `templates/base.html`.
- Static assets live in `static/`, including `static/styles.css` and `static/solcor-logo.png`.

## Persistence

- Database: SQLite via Python stdlib `sqlite3`.
- Database helper module: `monitoring_board/db.py`.
- Runtime database location is `monitoring_board.db` in the repo root by default, or `$DATA_DIR/monitoring_board.db` when `DATA_DIR` is configured; path logic lives in `monitoring_board/runtime.py`.
- SQLite is configured with WAL mode and conservative pragmas: `journal_mode=WAL`, `busy_timeout=10000`, `foreign_keys=ON`, `synchronous=NORMAL`, and `temp_store=MEMORY` in `monitoring_board/db.py` and `app.py`.
- Schema is created and evolved in code, not migrations: `ensure_database()` in `app.py` creates tables/views and calls `ensure_column()` for incremental columns.
- Field-route schema is initialized by the blueprint registration hook in `monitoring_board/routes/field_routes.py`.
- Key tables include `assets`, `asset_aliases`, `monitoring_records`, `monitoring_import_batches`, `om_contracts`, `integration_configs`, `asset_integrations`, `integration_sync_runs`, `integration_unresolved`, `production_records`, `performance_settings`, `telegram_alerts`, `alert_settings`, `app_state`, `background_jobs`, `alert_blacklist`, `tickets`, `ticket_visits`, `field_route_plans`, `field_route_stops`, and `field_route_segments`.
- The `latest_monitoring_view` view is created in `app.py` for latest asset status queries.

## Background Work

- Scheduler: APScheduler `BackgroundScheduler` (`apscheduler>=3.11.0`) imported and configured in `app.py`.
- Scheduler timezone is hardcoded to `Europe/Lisbon` in `start_integration_scheduler()`.
- Scheduled jobs include provider syncs and optional Telegram daily summary in `refresh_integration_scheduler()`.
- Ad hoc background jobs are persisted in the `background_jobs` table and scheduled with APScheduler date triggers by `schedule_background_job()` / `run_background_job()` in `app.py`.
- Production guidance intentionally uses one Gunicorn worker because APScheduler is in-process; this is documented in `README.md`, `Dockerfile`, `docker-compose.yml`, and `docs/raspberry-pi-deployment.md`.

## Dependencies

- `flask>=3.1.3`: web app, sessions, routing, templating.
- `requests>=2.32.5`: HTTP calls to FusionSolar, Sigenergy, Telegram, OpenRouteService, and Google My Maps KML URLs.
- `apscheduler>=3.11.0`: in-process scheduled sync and background job execution.
- `openpyxl>=3.1.5`: Excel import/export, with `load_workbook` and `Workbook` used in `app.py`.
- `reportlab>=4.4.10`: PDF export/report generation in `app.py`.
- `gunicorn>=23.0.0`: production WSGI server.
- `pytest>=8.0.0`: test runner.

## Configuration

- `.env` is manually loaded by `monitoring_board/runtime.py`; it only sets keys that are not already present in `os.environ`.
- Example configuration is in `.env.example`.
- Core app configuration keys include `FLASK_SECRET_KEY`, `APP_USERNAME`, `APP_PASSWORD`, `APP_PASSWORD_HASH`, `DATA_DIR`, `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`, and `MAX_UPLOAD_MB`.
- Integration configuration keys include `FUSIONSOLAR_*`, `SIGENERGY_*`, `TELEGRAM_*`, `OPENROUTESERVICE_API_KEY`, and default depot values for route planning.
- Docker Compose uses `.env`, sets `DATA_DIR=/data`, sets `SESSION_COOKIE_SECURE=true`, mounts `./data:/data`, and binds `127.0.0.1:5000:5000`.

## Filesystem Layout

- Runtime path resolution is centralized in `monitoring_board/runtime.py`.
- Runtime directories are created at startup for data, uploads, contracts, backups, and logs.
- Contract PDFs are stored under `uploads/contracts` by default or `$DATA_DIR/uploads/contracts` when `DATA_DIR` is set.
- Logs use `RotatingFileHandler` in `monitoring_board/logging_config.py`, writing `monitoring_board.log` under the runtime logs directory.
- Backups are created by `scripts/backup.sh`, using `sqlite3 ".backup"` for consistent SQLite snapshots and optional `tar` archives for uploads.

## File Workflows

- Excel import reads `.xlsx` / `.xlsm` using `openpyxl`; startup also detects the first root-level `.xlsx` as `DEFAULT_EXCEL_PATH` in `monitoring_board/runtime.py`.
- Generic exports can return XLSX or PDF from `export_rows_file()` in `app.py`.
- Customer production reports are PDF files built with ReportLab canvas in `export_customer_production_pdf()` in `app.py`.
- Contract upload/download is handled in asset routes in `app.py`; uploads validate PDF extension and magic bytes, store paths relative to runtime data, and serve files through `send_file()`.
- Field-route plans can export CSV through `monitoring_board/routes/field_routes.py`.

## Testing

- Tests live in `tests/` and run with `python -m pytest -q`.
- Test coverage includes DB helpers, runtime paths, security, field routes, Telegram alert policy, FusionSolar service/sync behavior, performance calculations/backfill/debug, link audit, and executive reports.
- Tests use direct module imports from `app.py` and service/helper modules rather than a separate package installation step.

## Operational Constraints

- The project intentionally avoids heavier infrastructure such as Celery, Redis, Postgres, or Kubernetes.
- SQLite and in-process APScheduler make single-process deployment important.
- Cloudflare Tunnel is the intended remote access path; the app itself is still protected by its own password login.
