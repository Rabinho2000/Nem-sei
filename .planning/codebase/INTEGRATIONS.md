# External Integrations

## SQLite Database

- Provider/type: local SQLite database file.
- Implementation files: `monitoring_board/db.py`, `monitoring_board/runtime.py`, `app.py`, and `monitoring_board/routes/field_routes.py`.
- Runtime location: repo-root `monitoring_board.db` by default, or `$DATA_DIR/monitoring_board.db` in Docker/Raspberry Pi deployment.
- Connection behavior: `get_db()` sets row factory, foreign keys, busy timeout, `synchronous=NORMAL`, and `temp_store=MEMORY`; runtime setup enables WAL.
- Schema ownership: `ensure_database()` in `app.py` and `ensure_field_routes_schema()` in `monitoring_board/routes/field_routes.py`.
- Backup integration: `scripts/backup.sh` uses the host `sqlite3` CLI `.backup` command and integrity checks.

## FusionSolar Northbound API

- Provider/type: Huawei FusionSolar / SmartPVMS Northbound API.
- Implementation files: main sync logic in `app.py`; helper normalization in `monitoring_board/services/fusionsolar.py`; UI partials under `templates/integrations/`.
- Configuration keys: `FUSIONSOLAR_USERNAME`, `FUSIONSOLAR_PASSWORD`, `FUSIONSOLAR_BASE_URL`, `FUSIONSOLAR_LOGIN_ENDPOINT`, `FUSIONSOLAR_STATIONS_ENDPOINT`, `FUSIONSOLAR_REALTIME_ENDPOINT`, `FUSIONSOLAR_ALARMS_ENDPOINT`, `FUSIONSOLAR_DAY_KPI_ENDPOINT`, `FUSIONSOLAR_MONTH_KPI_ENDPOINT`, and `FUSIONSOLAR_SYNC_HOURS`.
- Default endpoints in `app.py`: `/thirdData/login`, `/thirdData/stations`, `/thirdData/getStationRealKpi`, `/thirdData/getAlarmList`, `/thirdData/getKpiStationDay`, and `/thirdData/getKpiStationMonth`.
- Auth flow: `get_fusionsolar_session()` logs in with JSON `userName` and `systemCode`, extracts `XSRF-TOKEN` from headers/cookies, stores it on a `requests.Session`, and caches the session for about 25 minutes.
- API calls: station listing is paginated, realtime/KPI/alarm requests are batched by station code chunks of 100, and responses are validated for `success=True` and `failCode=0`.
- Data stored locally: station mappings in `asset_integrations`, unresolved external rows in `integration_unresolved`, sync history in `integration_sync_runs`, monitoring status in `monitoring_records`, and production KPIs in `production_records`.
- Scheduling: provider sync schedules come from `integration_configs.sync_hours`; `refresh_integration_scheduler()` registers cron jobs in APScheduler.
- Rate limits: production KPI paths track cooldown state in memory and `app_state`, with Telegram notification support for API limit events.
- Known assumptions/risk: API response shape, units, pagination, station code semantics, and rate-limit behavior are handled defensively in code but should be verified against the current FusionSolar API reference before relying on new fields.

## Sigenergy API

- Provider/type: Sigenergy / mySigen cloud API.
- Implementation file: `app.py`; integration settings share the `integration_configs`, `asset_integrations`, `integration_sync_runs`, and `integration_unresolved` tables.
- Configuration keys: `SIGENERGY_ENABLED`, `SIGENERGY_APP_KEY`, `SIGENERGY_APP_SECRET`, `SIGENERGY_BASE_URL`, `SIGENERGY_AUTH_ENDPOINT`, `SIGENERGY_SYSTEMS_ENDPOINT`, `SIGENERGY_REALTIME_ENDPOINT`, `SIGENERGY_ENERGY_FLOW_ENDPOINT`, `SIGENERGY_REGION`, `SIGENERGY_SYSTEM_IDS`, `SIGENERGY_SYSTEM_ID`, and `SIGENERGY_SYNC_HOURS`.
- Defaults in `.env.example` / `app.py`: base URL `https://api-eu.sigencloud.com`, auth endpoint `/openapi/auth/login/key`, systems endpoint `/openapi/system/list`, realtime endpoint `/openapi/system/realtime/data`, energy flow endpoint `/openapi/systems/{system_id}/energyFlow`, and region `eu`.
- Auth flow: `get_sigenergy_token()` base64-encodes `app_key:app_secret`, posts it as JSON `key`, validates response code, extracts `accessToken`, and caches it until shortly before expiry.
- Request headers: `Authorization: Bearer <token>`, `sigen-region`, `Accept: application/json`, and `Content-Type: application/json`.
- Fallback behavior: configured `SIGENERGY_SYSTEM_IDS` bypasses automatic system listing when the account/API does not expose systems.
- Data flow: fetched systems, realtime data, and energy-flow values are normalized to the same provider-row shape used by the generic integration sync path.

## Telegram Bot API

- Provider/type: Telegram Bot API.
- Implementation file: `monitoring_board/services/telegram_service.py`; alert orchestration and persistence are in `app.py`.
- Endpoint: `https://api.telegram.org/bot{token}/sendMessage`.
- Configuration keys: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALERTS_ENABLED`, and `TELEGRAM_DAILY_SUMMARY_ENABLED`.
- Message format: HTML parse mode.
- Local state: sent/blocked/failed alerts are stored in `telegram_alerts`; alert policy/settings use `alert_settings`, `alert_blacklist`, `alert_baseline`, asset alert flags, and app state keys.
- Scheduled behavior: if daily summaries are enabled, APScheduler registers `telegram-daily-summary` at 09:00 Europe/Lisbon.
- Failure handling: send failures are logged and alert records can capture failed/blocked status.

## OpenRouteService

- Provider/type: routing and geocoding API for field route planning.
- Implementation file: `monitoring_board/routes/field_routes.py`.
- Endpoints: `https://api.openrouteservice.org/v2/directions/driving-car/geojson` and `https://api.openrouteservice.org/geocode/search`.
- Configuration key: `OPENROUTESERVICE_API_KEY`.
- Usage: route segment distance/duration/geometry calculation, whole-route optimization enrichment, and asset coordinate geocoding.
- Fallback behavior: when no API key is configured or the API call fails, route distance falls back to local Haversine calculations with estimated duration.
- Stored data: route plans, stops, segments, geometry JSON, provider name, costs, and asset coordinates are stored in SQLite tables/columns created by `ensure_field_routes_schema()`.

## Google My Maps KML

- Provider/type: public KML download from Google My Maps links.
- Implementation file: `monitoring_board/routes/field_routes.py`.
- Usage: `download_mymaps_kml()` converts supported My Maps URLs into KML URLs, fetches KML over HTTP, validates the response, parses placemarks with `xml.etree.ElementTree`, and imports points into asset route coordinates.
- Auth: no app-side Google auth; the map must be publicly accessible.
- Risk: depends on public KML availability and URL shape.

## Cloudflare Tunnel / Access

- Provider/type: deployment/network access layer, outside application runtime.
- Documentation files: `README.md` and `docs/raspberry-pi-deployment.md`.
- Intended origin: `http://127.0.0.1:5000`.
- Compose binding: `127.0.0.1:5000:5000` in `docker-compose.yml`, deliberately not exposed on `0.0.0.0`.
- Access control: Cloudflare Access is recommended in front of the tunnel; the Flask app still uses its own username/password login.
- No direct Cloudflare SDK/API integration exists in application code.

## Application Authentication

- Provider/type: local password authentication, not an external identity provider.
- Implementation files: `monitoring_board/security.py` and `monitoring_board/routes/auth.py`.
- Configuration keys: `APP_USERNAME`, `APP_PASSWORD`, `APP_PASSWORD_HASH`, and `FLASK_SECRET_KEY`.
- Password verification: Werkzeug `check_password_hash()` for `APP_PASSWORD_HASH`, or constant-time comparison against `APP_PASSWORD`.
- Session/CSRF: Flask session stores authentication state and a CSRF token; POST requests are protected in `app.py`.
- No OAuth, SAML, LDAP, or external auth provider integration is present.

## File and Report Integrations

- Excel: `openpyxl` reads imports and writes XLSX exports in `app.py`; root-level Excel files may be auto-discovered through `DEFAULT_EXCEL_PATH`.
- PDF: `reportlab` creates generic PDF exports and customer production reports in `app.py`.
- Contract storage: uploaded PDF contracts are stored in runtime uploads and referenced from `om_contracts`.
- CSV: field route plan CSV exports are generated in `monitoring_board/routes/field_routes.py`.
- No external object storage is currently integrated; runtime files remain on local disk or the Docker-mounted `./data` directory.

## HTTP Client Surface

- All outbound HTTP uses `requests`.
- Timeouts are set on external calls: typical values are 30 seconds for FusionSolar/Sigenergy/My Maps KML, 20 seconds for OpenRouteService directions, 15 seconds for OpenRouteService geocoding, and 12 seconds for Telegram sends.
- There is no webhook receiver or outgoing webhook framework in the current codebase.
