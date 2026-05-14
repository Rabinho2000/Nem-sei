# Coding Conventions

## Scope

This map covers code style, naming, application patterns, and error handling conventions in the Flask monitoring board.

## Overall Style

- The codebase is Python-first, procedural, and explicit. Most business behavior lives in top-level functions rather than classes.
- `app.py` is the primary application module and contains Flask app creation, schema bootstrap, routes, reporting, integrations, background jobs, and many domain helpers.
- Smaller extracted modules live under `monitoring_board/`, especially reusable helpers in `monitoring_board/db.py`, `monitoring_board/runtime.py`, `monitoring_board/security.py`, and `monitoring_board/services/`.
- Files consistently use `from __future__ import annotations` in Python modules and tests.
- Type hints are common for new helpers: examples include `create_app() -> Flask` in `app.py`, `get_db(path: str) -> sqlite3.Connection` in `monitoring_board/db.py`, and route helpers in `monitoring_board/routes/field_routes.py`.
- Domain data is usually represented as dictionaries, `sqlite3.Row`, tuples, or small frozen dataclasses, not ORM models.
- Frozen dataclasses are used where a structured in-memory value clarifies routing logic, e.g. `RouteStop`, `RoutePoint`, and `RouteSegment` in `monitoring_board/routes/field_routes.py`.

## Naming

- Constants use uppercase snake case at module level, e.g. `INTEGRATION_PROVIDER_FUSIONSOLAR`, `BACKGROUND_JOB_TYPES_PERFORMANCE`, and `ALERT_SETTING_DEFAULTS` in `app.py`.
- Functions use descriptive snake case and usually start with a verb: `ensure_database`, `normalize_sync_hours`, `build_provider_url`, `fetch_route_assets`, `calculate_expected_production`.
- Boolean helpers read as predicates: `app_password_configured`, `telegram_alerts_enabled`, `path_is_within`, `is_fusionsolar_rate_limit_error`.
- Database helper names distinguish query shape: `query_all` returns rows and `query_scalar` returns a single value in `monitoring_board/db.py`.
- Route endpoint names are direct nouns or verbs aligned with UI sections, e.g. `dashboard`, `assets`, `performance`, `integrations`, and `field_routes.field_routes`.
- Test names follow `test_<behavior>` and are behavior-specific rather than implementation-specific.

## Flask Patterns

- App setup is centralized in `create_app()` in `app.py`; the module also exposes `app = create_app()` for Gunicorn and tests.
- Blueprints are used for extracted route groups: `auth_bp` in `monitoring_board/routes/auth.py` and `field_routes_bp` in `monitoring_board/routes/field_routes.py`.
- Most legacy/main routes still live directly in `app.py`.
- `before_request` opens `g.db`, records request start time, enforces CSRF on every `POST`, and redirects unauthenticated requests to login.
- `teardown_request` logs request duration and closes `g.db`.
- Views generally validate form input, write through `g.db`, call `commit()`, `flash()` a Portuguese user message, then `redirect()`.
- Templates extend or share `templates/base.html`; UI state is passed through `render_template()` context dictionaries and global context processors.
- Client-side behavior is mostly plain JavaScript embedded in `templates/base.html` or specific templates, not bundled frontend tooling.

## SQLite Patterns

- SQLite access is direct through `sqlite3`, with no ORM.
- Connections should come from `get_db()` in `monitoring_board/db.py`, which sets `row_factory`, foreign keys, busy timeout, and conservative PRAGMAs.
- Runtime bootstrap calls `configure_database_for_runtime()` to enable WAL and related PRAGMAs.
- Schema creation and migrations are idempotent and embedded in code using `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and `ensure_column()`.
- Long schema bootstrap lives in `ensure_database()` and `ensure_database_indexes()` in `app.py`; field-route-specific schema lives in `ensure_field_routes_schema()` in `monitoring_board/routes/field_routes.py`.
- SQL parameters are normally passed with `?` placeholders. Dynamic SQL appears for controlled identifiers or placeholder lists; keep future dynamic SQL tightly constrained.
- Commits are explicit and usually performed by the caller or route after a group of writes.
- Connections in tests and background work are closed with `finally` blocks or `contextlib.closing`.

## Runtime And Configuration

- Runtime paths are centralized in `monitoring_board/runtime.py`.
- `.env` is loaded manually by `load_local_env()` before runtime constants are resolved.
- Environment flags accept Portuguese-friendly truthy values through `env_flag()` and service-specific helpers.
- Secrets are read from environment variables; `.env`, databases, uploads, reports, logs, and local runtime files are not application code.
- Production deployment is expected to run one Gunicorn worker because APScheduler is in-process; this is documented in `README.md`.

## Integration Patterns

- FusionSolar and Sigenergy logic in `app.py` uses module-level endpoint constants, normalized provider config, and direct `requests` calls.
- Reusable FusionSolar helpers that are easy to unit-test live in `monitoring_board/services/fusionsolar.py`.
- Telegram sending is isolated in `monitoring_board/services/telegram_service.py`, returning booleans and logging failures instead of raising to callers.
- HTTP calls use explicit timeouts and usually call `raise_for_status()`.
- External API failures are usually caught as `requests.RequestException` or `ValueError`, logged, and converted into `None`, `False`, `ValueError`, or user-facing flash messages depending on context.

## Background Jobs

- APScheduler is stored in module global `SCHEDULER` in `app.py`.
- Scheduler startup is guarded by `if SCHEDULER is not None: return` in `start_integration_scheduler()`.
- Scheduled jobs are registered with stable IDs and `replace_existing=True`.
- Request-triggered heavy work is represented as rows in `background_jobs`; helper functions create, mark running, mark success, mark failed, and recover stale running jobs.
- Tests expect performance sync and backfill routes to queue jobs rather than run expensive work inline.

## Error Handling

- User-facing validation errors typically use `flash(..., "error")` or `flash(..., "warning")` followed by a redirect.
- Security failures use `abort(400)` for invalid CSRF and `abort(404)` for forbidden file access patterns.
- Configuration errors that should block startup raise `RuntimeError`, e.g. insecure `FLASK_SECRET_KEY` in `monitoring_board/security.py`.
- Domain parsing functions often return `None` for invalid optional values, e.g. `parse_optional_float()` and `parse_date_value()`.
- External integration helpers log warnings for recoverable API failures and continue with fallback behavior when possible.
- Background job failures are caught broadly, logged with stack traces, and persisted as failed job rows.

## UI And Templates

- Templates are server-rendered Jinja files under `templates/`.
- Portuguese is the dominant UI language.
- Common layout, navigation, CSRF injection, theme toggle, and auto-submit behavior live in `templates/base.html`.
- CSS uses custom properties for light/dark themes in `static/styles.css`.
- Visual components use recurring class names such as `shell`, `sidebar`, `content`, `hero`, `card`, `button`, `flash`, `grid`, and `chips`.

## Practical Guidance

- Keep changes small and local; avoid sweeping refactors in `app.py` unless a task explicitly calls for extraction.
- Prefer adding focused helpers beside related existing helpers, then cover them with tests.
- Preserve explicit SQLite connection ownership and commit boundaries.
- Keep scheduled work single-process aware; do not add patterns that assume multiple workers can safely run in-process jobs.
- For API behavior, units, dates, and response shapes, add tests around normalization and fallback behavior before relying on assumptions.
- When adding routes, mirror existing validation, flash, redirect, CSRF, and database lifecycle patterns.
