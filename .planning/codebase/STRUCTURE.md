# Structure Map

## Top-Level Layout

```text
.
├── app.py
├── monitoring_board/
├── templates/
├── static/
├── tests/
├── docs/
├── scripts/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── README.md
├── AGENTS.md
└── .planning/
```

The repository root also contains local/runtime artifacts in this working copy, including `.env`, `monitoring_board.db`, `monitoring_board.db-wal`, `monitoring_board.db-shm`, `uploads/`, `backups/`, `logs/`, `reports/`, a FusionSolar API PDF, and a source Excel workbook. These are operational data and should not be treated as code structure.

## Application Root

`app.py` is the central application file. It contains:

- Flask app factory: `create_app()`.
- Global `app = create_app()` WSGI object.
- CLI local startup via `parse_cli_args()` and `if __name__ == "__main__"`.
- Main route handlers for dashboard, assets, monitoring, tickets, exports, integrations, Telegram alerts, renewals, settings, and performance.
- SQLite schema creation in `ensure_database()`.
- Index creation in `ensure_database_indexes()`.
- Background job queue helpers.
- Integration sync, report generation, import/export, alerting, and performance calculations.

Important route locations in `app.py`:

- `/` dashboard: `dashboard()`.
- `/performance`, `/performance/debug/<id>`, `/performance/backfill`: performance views and job enqueueing.
- `/assets` and `/asset/<id>` routes: asset CRUD, aliases, contract upload/open, performance settings, group metadata.
- `/monitoring`: daily monitoring import and record management.
- `/tickets`: ticket and visit management.
- `/exports`: CSV/XLSX/PDF export workflows.
- `/integrations`: FusionSolar/Sigenergy/Telegram config, sync, mappings, unresolved rows, alert settings.
- `/telegram-alerts`: alert audit view.
- `/renewals`: contract renewal tracking.
- `/settings`: Excel import and database summary.

## Package Directory

`monitoring_board/` is the extracted Python package.

```text
monitoring_board/
├── __init__.py
├── db.py
├── logging_config.py
├── runtime.py
├── security.py
├── routes/
│   ├── __init__.py
│   ├── auth.py
│   └── field_routes.py
└── services/
    ├── __init__.py
    ├── fusionsolar.py
    └── telegram_service.py
```

Key files:

- `monitoring_board/db.py`: SQLite connection setup, WAL PRAGMAs, backups, `ensure_column()`, small query helpers.
- `monitoring_board/runtime.py`: `.env` loading, `DATA_DIR` resolution, runtime directories, upload/backup/log/database path helpers.
- `monitoring_board/security.py`: app username/password validation, Flask secret validation, CSRF token helper.
- `monitoring_board/logging_config.py`: rotating log configuration.
- `monitoring_board/routes/auth.py`: `auth_bp`, login/logout routes, safe local redirect helper.
- `monitoring_board/routes/field_routes.py`: `field_routes_bp`, route planning schema, route-plan UI, OpenRouteService integration, KML import, route CSV export.
- `monitoring_board/services/fusionsolar.py`: provider URL building, sync-hour normalization, FusionSolar status normalization.
- `monitoring_board/services/telegram_service.py`: Telegram config, masking, send/test helpers.

## Template Directory

`templates/` contains server-rendered Jinja views. The convention is one main template per route/view, with underscore-prefixed partials for reusable fragments.

```text
templates/
├── base.html
├── dashboard.html
├── assets.html
├── asset_detail.html
├── monitoring.html
├── tickets.html
├── exports.html
├── integrations.html
├── login.html
├── error.html
├── settings.html
├── renewals.html
├── performance.html
├── performance_debug.html
├── performance_backfill.html
├── telegram_alerts.html
├── field_routes.html
├── field_route_plan.html
├── _background_jobs.html
├── _performance_bar.html
└── integrations/
```

`templates/integrations/` contains partials used by `templates/integrations.html`:

- `_hero.html`
- `_sync_status.html`
- `_sync_history.html`
- `_fusionsolar_config.html`
- `_fusionsolar_mappings.html`
- `_fusionsolar_link_audit.html`
- `_unresolved.html`
- `_telegram.html`
- `_alert_filters.html`
- `_alert_blacklist.html`
- `_bulk_alerts.html`
- `_asset_options.html`

Naming conventions:

- Full page templates use route/domain names, for example `performance.html` and `field_routes.html`.
- Partials begin with `_`.
- Integration partials are grouped under `templates/integrations/`.

## Static Assets

`static/` contains:

- `static/styles.css`: main stylesheet for the server-rendered UI.
- `static/solcor-logo.png`: logo asset used by UI/reporting.

There is no frontend build step or JavaScript package structure in the current repository.

## Tests

`tests/` contains pytest coverage for extracted helpers and high-risk application logic.

Current test files include:

- `tests/test_security.py`: login/CSRF/security behavior.
- `tests/test_db_helpers.py`: database helper behavior.
- `tests/test_runtime_paths.py`: runtime path and data directory behavior.
- `tests/test_fusionsolar_service.py`: service-level FusionSolar helpers.
- `tests/test_fusionsolar_sync.py`: sync state and monitoring behavior.
- `tests/test_fusionsolar_link_audit.py`: provider-to-asset mapping audit behavior.
- `tests/test_telegram_alert_policy.py`: alert filtering/throttling behavior.
- `tests/test_performance.py`: production/performance calculations and report behavior.
- `tests/test_performance_backfill.py`: backfill, rate-limit, and reference recalculation behavior.
- `tests/test_performance_debug.py`: performance debug and background job enqueue routes.
- `tests/test_performance_references.py`: expected production/reference behavior.
- `tests/test_executive_report.py`: executive report behavior.
- `tests/test_field_routes.py`: route planning/geocoding/field route behavior.

Tests commonly import directly from `app.py`, create temporary SQLite databases, call `ensure_database()`, and use Flask test clients.

## Documentation And Operations

- `README.md`: local setup, security notes, Docker/Raspberry Pi deployment summary, backup/restore notes, runtime data directory behavior.
- `docs/raspberry-pi-deployment.md`: deployment guide for Raspberry Pi.
- `scripts/backup.sh`: file-based SQLite/uploads backup script intended for Raspberry Pi/Docker runtime data.
- `Dockerfile`: Python container build.
- `docker-compose.yml`: single-service deployment, mounts `./data:/data`, publishes `127.0.0.1:5000:5000`, and runs Gunicorn with one worker.
- `.env.example`: environment variable template.
- `.gitignore` and `.dockerignore`: local/runtime artifact exclusions.
- `AGENTS.md`: repository-specific guidance for coding agents.

## Runtime Data Locations

Without `DATA_DIR`, runtime paths resolve to the repository root:

- `monitoring_board.db`
- `uploads/`
- `uploads/contracts/`
- `backups/`
- `logs/`

With `DATA_DIR`, paths resolve under that directory. Docker Compose sets `DATA_DIR=/data`, so persistent runtime data is expected under the host `./data` mount.

Path helpers in `monitoring_board/runtime.py` should be used when working with stored upload/contract paths because stored paths may be runtime-relative.

## Naming And Organization Conventions

- Python modules use snake_case names.
- Flask blueprint variables use `_bp`, for example `auth_bp` and `field_routes_bp`.
- Database tables use plural snake_case names.
- Provider names are stored as display strings, currently `FusionSolar` and `Sigenergy`.
- Status values are Portuguese display strings such as `Operacional`, `Erro`, `Desconectada`, `Resolvido`, `Atenção`, `Alerta`, and `Crítico`.
- HTML templates use domain/page names; partials are prefixed with `_`.
- Tests are named `test_<domain>.py` and test functions use descriptive `test_...` names.

## Practical Navigation

For dashboard and high-level summary logic, start in `app.py` near `dashboard()` and the helper functions around `fetch_dashboard_stats()`.

For data model changes, start in `app.py` at `ensure_database()` and then check `monitoring_board/routes/field_routes.py` for field-route-specific schema.

For request-level security behavior, inspect `create_app()` in `app.py`, then `monitoring_board/security.py` and `monitoring_board/routes/auth.py`.

For database connection behavior, inspect `monitoring_board/db.py`.

For runtime file locations and Docker data directory behavior, inspect `monitoring_board/runtime.py`, `README.md`, and `docker-compose.yml`.

For FusionSolar/Sigenergy sync behavior, start in `app.py` around `run_fusionsolar_sync()`, `run_provider_check()`, and the provider fetch/normalize helpers. Small shared FusionSolar helpers live in `monitoring_board/services/fusionsolar.py`.

For performance production sync and backfills, inspect `app.py` around `run_fusionsolar_production_sync()`, `run_fusionsolar_production_backfill()`, `run_fusionsolar_month_cycle()`, and `recalculate_performance_references()`.

For Telegram delivery, inspect `monitoring_board/services/telegram_service.py` for the API call and `app.py` for alert policy, cooldowns, and message generation.

For route planning, inspect `monitoring_board/routes/field_routes.py` and the templates `templates/field_routes.html` and `templates/field_route_plan.html`.
