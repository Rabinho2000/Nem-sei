# Testing Patterns

## Scope

This map covers the repository's test framework, structure, mocking patterns, database setup, and coverage focus.

## Framework

- Tests use `pytest`; it is listed directly in `requirements.txt`.
- The documented command is `python -m pytest -q` in `README.md`.
- There is no `pytest.ini`, `pyproject.toml`, coverage configuration, or visible pytest plugin setup.
- Tests are plain function tests under `tests/`, not class-based suites.
- Most test modules start with `from __future__ import annotations`.

## Test Layout

- Test files are grouped by feature or risk area:
- `tests/test_security.py` covers login, CSRF, session cookie defaults, redirect safety, upload validation, and path traversal.
- `tests/test_db_helpers.py` covers SQLite helper PRAGMAs, idempotent schema changes, indexes, calendar grouping, and background job state helpers.
- `tests/test_runtime_paths.py` covers `DATA_DIR` path resolution and runtime directory creation.
- `tests/test_fusionsolar_service.py`, `tests/test_fusionsolar_sync.py`, and `tests/test_fusionsolar_link_audit.py` cover provider normalization, sync behavior, and mapping audits.
- `tests/test_telegram_alert_policy.py` covers alert gating, blacklist/silence behavior, deduplication, aggregation, and alarm summaries.
- `tests/test_performance.py`, `tests/test_performance_backfill.py`, `tests/test_performance_debug.py`, and `tests/test_performance_references.py` cover production records, calculations, reporting, backfill, rate limits, diagnostics, and async job queuing.
- `tests/test_field_routes.py` covers field route schema, route ordering, geocoding, OpenRouteService fallback, Google My Maps import, and route form behavior.
- `tests/test_executive_report.py` covers executive and monitoring report row calculations.

## Fixtures And Test Data

- Tests usually create temporary SQLite databases with `tmp_path`.
- Full-app schema tests call `ensure_database(str(db_path))`, then open connections with `get_db(str(db_path))`.
- Some narrow schema tests create only the minimal tables needed before calling feature-specific schema helpers such as `ensure_field_routes_schema()`.
- Fixtures are local to test modules. Examples: `conn(tmp_path)` in `tests/test_performance.py` and `tests/test_performance_backfill.py`.
- There is no shared `tests/conftest.py`; helper functions such as `add_asset()`, `make_conn()`, `fake_session()`, and `kpi_row()` are module-local.
- Test data is inserted directly with SQL, often using multi-line SQL strings to mirror production table shape.
- Tests commonly commit setup data before invoking code paths that expect persisted rows.
- Connections are closed in `finally` blocks or fixture teardown.

## Flask Testing

- Flask route tests use `app.test_client()` from the global `app` object in `app.py`.
- Tests that need authentication set session values through `client.session_transaction()`.
- Tests that need CSRF either extract the token from the login page or set `sess["csrf_token"] = "token"` and post the matching form field.
- Tests that need an isolated database temporarily override `flask_app.config["DATABASE"]`, then restore the previous value in `finally`.
- Some route-unit tests use `Flask(__name__)` and `app.test_request_context()` to call route helpers directly without the full application.
- Template rendering is sometimes monkeypatched to capture context instead of asserting rendered HTML.

## Mocking And Isolation

- Mocking uses pytest's built-in `monkeypatch` fixture.
- Environment variables are set with `monkeypatch.setenv()`, especially for Telegram, FusionSolar, depot defaults, and security config.
- External HTTP is avoided in tests by monkeypatching request helpers or `requests.get`.
- Fake response classes implement only the methods production code needs, commonly `raise_for_status()` and `json()`.
- Integration sessions are faked by monkeypatching functions such as `get_fusionsolar_session()`.
- Expensive or unsafe inline work is guarded with fake functions that raise `AssertionError` if called; see performance job queue tests in `tests/test_performance_debug.py`.
- Sleeps and rate-limit waits are made testable through injected delay values or monkeypatched fetch functions.
- No dedicated HTTP mocking library is used.

## Assertion Style

- Assertions are direct `assert` statements.
- Tests verify observable behavior: rows written, statuses selected, redirects returned, HTML escaped, jobs queued, and alerts blocked or sent.
- Database assertions query SQLite directly and inspect `sqlite3.Row` fields.
- Route tests assert status codes, redirect locations, response bytes, and persisted side effects.
- Security tests include negative assertions, e.g. unsafe script text should be escaped and unsafe raw HTML should not appear.
- Date-sensitive behavior is usually controlled by explicit `date(...)` or `datetime(...)` values, not by global time-freezing libraries.

## Coverage Focus

- Strong coverage exists for business logic around performance calculations, production backfill, reference recalculation, alert policy, schema idempotency, security, and route planning.
- SQLite behavior is tested beyond simple CRUD, including WAL-related PRAGMAs, indexes, idempotent columns, stale background job recovery, and path safety.
- Integration code is tested mainly through normalization and orchestration boundaries, with external APIs mocked out.
- Tests check important operational constraints: background jobs should not run inline in request handlers, repeated FusionSolar rate-limit alerts should be throttled, and duplicate background jobs should not be created unnecessarily.
- PDF/report behavior has targeted checks for local production data selection, but broad visual PDF verification is not present.
- Template and CSS coverage is limited; UI assertions are mostly route smoke checks and selected HTML/text safety checks.

## Gaps And Risks

- There is no measured coverage threshold or coverage report configuration.
- There is no shared fixture layer, so setup patterns are duplicated across test modules.
- Tests import `app.py`, which creates the Flask app and starts application bootstrap side effects; this can make tests sensitive to module import behavior.
- The global APScheduler and module-level caches are not broadly isolated in tests; some tests manually reset specific globals such as `FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL`.
- Current tests avoid real external APIs, which is correct for unit tests, but means API pagination, rate limits, token refresh edge cases, and response shape drift need careful mocked scenarios when changed.
- There are few frontend interaction tests and no browser automation tests.
- There are no explicit concurrency tests for SQLite locking or multi-request/background-job write contention.

## Practical Guidance

- Add tests when changing scheduling, database writes, API parsing, calculations, reports, alerts, imports, uploads, or security-sensitive paths.
- Prefer `tmp_path` databases and `ensure_database()` for integration-like tests.
- Use `get_db()` in tests so PRAGMA behavior matches application connections.
- Use `monkeypatch` for environment variables, network calls, scheduler calls, and slow waits.
- For route tests, authenticate via `session_transaction()` and include a matching CSRF token for `POST`.
- Keep tests behavior-focused and assert persisted database state, not just function return values.
- When changing FusionSolar, Telegram, or OpenRouteService behavior, include mocked failure cases as well as success cases.
- When adding background work, test both the queueing route and the payload function separately.
