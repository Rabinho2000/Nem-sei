"""Validated V2 PostgreSQL runtime settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url


VALID_ENVIRONMENTS = {"development", "test", "preview", "production"}
PROCESS_ROLES = {"web", "scheduler", "worker", "migrate", "admin"}
CAPABILITIES = ("provider_reads", "provider_mutations", "notifications", "report_distribution")
INSECURE_SECRET_KEYS = {"changeme", "change-me", "secret", "development", "default"}
ROLE_DATABASE_DEFAULTS = {
    "web": {"pool_size": 2, "max_overflow": 1, "statement_timeout_ms": 15000, "lock_timeout_ms": 3000, "idle_transaction_timeout_ms": 30000},
    "scheduler": {"pool_size": 1, "max_overflow": 1, "statement_timeout_ms": 10000, "lock_timeout_ms": 3000, "idle_transaction_timeout_ms": 20000},
    "worker": {"pool_size": 2, "max_overflow": 1, "statement_timeout_ms": 30000, "lock_timeout_ms": 5000, "idle_transaction_timeout_ms": 60000},
    "migrate": {"pool_size": 1, "max_overflow": 0, "statement_timeout_ms": 120000, "lock_timeout_ms": 10000, "idle_transaction_timeout_ms": 120000},
    "admin": {"pool_size": 1, "max_overflow": 0, "statement_timeout_ms": 60000, "lock_timeout_ms": 10000, "idle_transaction_timeout_ms": 60000},
}


class ConfigurationError(ValueError):
    pass


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Expected boolean, got {value!r}.")


def _optional_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ConfigurationError(f"Expected an integer connection id, got {value!r}.") from exc


def read_secret_value(*, value_name: str, file_name: str) -> str:
    """Read one configuration secret from an env value or a mounted secret file."""
    value = os.environ.get(value_name, "").strip()
    file_value = os.environ.get(file_name, "").strip()
    if value and file_value:
        raise ConfigurationError(f"Set only one of {value_name} or {file_name}.")
    if value:
        return value
    if not file_value:
        return ""
    try:
        return Path(file_value).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"Unable to read {file_name}.") from exc


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str
    secret_key: str
    admin_username: str
    admin_password_hash: str
    capabilities: dict[str, bool]
    process_role: str = "web"
    worker_poll_seconds: float = 2.0
    worker_lease_seconds: int = 30
    scheduler_lease_seconds: int = 30
    db_pool_size: int = 2
    db_max_overflow: int = 1
    db_statement_timeout_ms: int = 15000
    db_lock_timeout_ms: int = 3000
    db_idle_transaction_timeout_ms: int = 30000
    db_pool_recycle_seconds: int = 1800
    production_max_source_days: int = 31
    production_reconciliation_max_source_days: int = 3
    production_backfill_max_source_days: int = 366
    production_backfill_chunk_days: int = 31
    # M7 Fatia 3 (docs/v2/DEVICE_TELEMETRY.md): a persistent device-status
    # poll schedule, off by default and restricted to one explicit
    # connection -- there is no "poll every active FusionSolar connection"
    # mode, so scaling to the portfolio requires a deliberate code change,
    # not a config flip. `device_status_poll_connection_id=None` means the
    # schedule is structurally inert even if `_enabled=True`.
    device_status_poll_enabled: bool = False
    device_status_poll_interval_minutes: int = 30
    device_status_poll_connection_id: int | None = None
    # A hard cap on the *lifetime* number of `device_status.poll` cycles ever
    # enqueued for `device_status_poll_connection_id` (counted from the jobs
    # table itself, not a separate counter -- see
    # `JobRepository.enqueue_due_device_status_poll`). Required whenever
    # polling is enabled, same structural pattern as the connection-id
    # requirement just above: there is no "enabled with no cap" mode, so an
    # unattended run can never run away unbounded even if nobody is watching
    # it. docs/v2/DEVICE_TELEMETRY.md §9.
    device_status_poll_max_cycles: int | None = None
    # Same structural shape, for `production.incremental` instead of device
    # status: off by default, restricted to one explicit connection (no
    # "sync every active FusionSolar connection" mode -- scaling to the
    # portfolio needs a deliberate second call site, not a config flip; see
    # docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md's rollout write-up for why
    # that restraint matters here specifically: a shared, rate-limited
    # account). No lifetime cap, unlike device-status polling -- this is
    # meant to run indefinitely once turned on, matching V1's own daily
    # `fusionsolar_production_sync` having none either.
    production_sync_scheduler_enabled: bool = False
    production_sync_scheduler_interval_hours: int = 24
    production_sync_scheduler_connection_id: int | None = None
    # Sigenergy's own account, own rate limits, own switch. Deliberately not
    # folded into the FusionSolar settings above: the two share nothing --
    # not the account, not the ownership broker, not the throttling -- and one
    # flag governing both would make turning on the quiet one also turn on the
    # contended one.
    sigenergy_sync_scheduler_enabled: bool = False
    sigenergy_sync_scheduler_interval_hours: int = 24
    sigenergy_sync_scheduler_connection_id: int | None = None
    # M7 Fatia 5 / D1 (docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md): the
    # periodic diagnostic-incident evaluator. Off by default, same pattern as
    # every other recurring behaviour this codebase adds -- but unlike
    # device-status polling this makes zero provider calls (it only reads
    # already-persisted device_status_facts and writes diagnostic_incidents),
    # so it needs neither a connection id nor a call-budget hard cap; it runs
    # across every asset that owns a device, by design.
    diagnostic_incident_evaluation_enabled: bool = False
    diagnostic_incident_evaluation_interval_minutes: int = 15
    # D3 (docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md): the periodic
    # notification-policy evaluator. Off by default. Also makes zero
    # provider calls (only reads diagnostic_incidents, writes
    # notification_events) -- and even once enabled, delivery can only ever
    # reach `notifications/telegram_client.py`'s mock: no real Telegram
    # client exists in this codebase yet (D4).
    notification_processing_enabled: bool = False
    notification_processing_interval_minutes: int = 15
    # D6 (docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md): the periodic
    # digest generator. Off by default, zero provider calls (reads
    # diagnostic_incidents via portfolios/diagnostics.py, writes
    # digest_runs). Daily by default (1440 min) -- a digest is a summary
    # over a window, not an immediate alert, so it does not need D1/D3's
    # 15-minute cadence.
    digest_generation_enabled: bool = False
    digest_generation_interval_minutes: int = 1440
    testing: bool = False

    @property
    def sqlalchemy_database_url(self) -> str:
        return self.database_url

    @classmethod
    def from_environment(cls) -> "Settings":
        environment = os.environ.get("NEMSEI_V2_ENV", "development").strip().lower()
        process_role = os.environ.get("NEMSEI_V2_PROCESS_ROLE", "web").strip().lower()
        defaults = ROLE_DATABASE_DEFAULTS.get(process_role, ROLE_DATABASE_DEFAULTS["web"])
        return cls(
            environment=environment,
            database_url=read_secret_value(
                value_name="NEMSEI_V2_DATABASE_URL",
                file_name="NEMSEI_V2_DATABASE_URL_FILE",
            ),
            secret_key=os.environ.get("NEMSEI_V2_SECRET_KEY", ""),
            admin_username=os.environ.get("NEMSEI_V2_ADMIN_USERNAME", "admin").strip(),
            admin_password_hash=os.environ.get("NEMSEI_V2_ADMIN_PASSWORD_HASH", "").strip(),
            capabilities={name: parse_bool(os.environ.get(f"NEMSEI_V2_{name.upper()}"), default=False) for name in CAPABILITIES},
            process_role=process_role,
            worker_poll_seconds=float(os.environ.get("NEMSEI_V2_WORKER_POLL_SECONDS", "2")),
            worker_lease_seconds=int(os.environ.get("NEMSEI_V2_WORKER_LEASE_SECONDS", "30")),
            scheduler_lease_seconds=int(os.environ.get("NEMSEI_V2_SCHEDULER_LEASE_SECONDS", "30")),
            db_pool_size=int(os.environ.get("NEMSEI_V2_DB_POOL_SIZE", str(defaults["pool_size"]))),
            db_max_overflow=int(os.environ.get("NEMSEI_V2_DB_MAX_OVERFLOW", str(defaults["max_overflow"]))),
            db_statement_timeout_ms=int(os.environ.get("NEMSEI_V2_DB_STATEMENT_TIMEOUT_MS", str(defaults["statement_timeout_ms"]))),
            db_lock_timeout_ms=int(os.environ.get("NEMSEI_V2_DB_LOCK_TIMEOUT_MS", str(defaults["lock_timeout_ms"]))),
            db_idle_transaction_timeout_ms=int(os.environ.get("NEMSEI_V2_DB_IDLE_TRANSACTION_TIMEOUT_MS", str(defaults["idle_transaction_timeout_ms"]))),
            db_pool_recycle_seconds=int(os.environ.get("NEMSEI_V2_DB_POOL_RECYCLE_SECONDS", "1800")),
            production_max_source_days=int(os.environ.get("NEMSEI_V2_PRODUCTION_MAX_SOURCE_DAYS", "31")),
            production_reconciliation_max_source_days=int(os.environ.get("NEMSEI_V2_PRODUCTION_RECONCILIATION_MAX_SOURCE_DAYS", "3")),
            production_backfill_max_source_days=int(os.environ.get("NEMSEI_V2_PRODUCTION_BACKFILL_MAX_SOURCE_DAYS", "366")),
            production_backfill_chunk_days=int(os.environ.get("NEMSEI_V2_PRODUCTION_BACKFILL_CHUNK_DAYS", "31")),
            device_status_poll_enabled=parse_bool(os.environ.get("NEMSEI_V2_DEVICE_STATUS_POLL_ENABLED"), default=False),
            device_status_poll_interval_minutes=int(os.environ.get("NEMSEI_V2_DEVICE_STATUS_POLL_INTERVAL_MINUTES", "30")),
            device_status_poll_connection_id=_optional_int(os.environ.get("NEMSEI_V2_DEVICE_STATUS_POLL_CONNECTION_ID")),
            device_status_poll_max_cycles=_optional_int(os.environ.get("NEMSEI_V2_DEVICE_STATUS_POLL_MAX_CYCLES")),
            production_sync_scheduler_enabled=parse_bool(os.environ.get("NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_ENABLED"), default=False),
            production_sync_scheduler_interval_hours=int(os.environ.get("NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_INTERVAL_HOURS", "24")),
            production_sync_scheduler_connection_id=_optional_int(os.environ.get("NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_CONNECTION_ID")),
            sigenergy_sync_scheduler_enabled=parse_bool(os.environ.get("NEMSEI_V2_SIGENERGY_SYNC_SCHEDULER_ENABLED"), default=False),
            sigenergy_sync_scheduler_interval_hours=int(os.environ.get("NEMSEI_V2_SIGENERGY_SYNC_SCHEDULER_INTERVAL_HOURS", "24")),
            sigenergy_sync_scheduler_connection_id=_optional_int(os.environ.get("NEMSEI_V2_SIGENERGY_SYNC_SCHEDULER_CONNECTION_ID")),
            diagnostic_incident_evaluation_enabled=parse_bool(
                os.environ.get("NEMSEI_V2_DIAGNOSTIC_INCIDENT_EVALUATION_ENABLED"), default=False
            ),
            diagnostic_incident_evaluation_interval_minutes=int(
                os.environ.get("NEMSEI_V2_DIAGNOSTIC_INCIDENT_EVALUATION_INTERVAL_MINUTES", "15")
            ),
            notification_processing_enabled=parse_bool(
                os.environ.get("NEMSEI_V2_NOTIFICATION_PROCESSING_ENABLED"), default=False
            ),
            notification_processing_interval_minutes=int(
                os.environ.get("NEMSEI_V2_NOTIFICATION_PROCESSING_INTERVAL_MINUTES", "15")
            ),
            digest_generation_enabled=parse_bool(os.environ.get("NEMSEI_V2_DIGEST_GENERATION_ENABLED"), default=False),
            digest_generation_interval_minutes=int(
                os.environ.get("NEMSEI_V2_DIGEST_GENERATION_INTERVAL_MINUTES", "1440")
            ),
            testing=parse_bool(os.environ.get("NEMSEI_V2_TESTING"), default=False),
        )

    def validate(self, *, require_auth: bool = False) -> "Settings":
        if self.environment not in VALID_ENVIRONMENTS:
            raise ConfigurationError("Invalid V2 environment.")
        if self.process_role not in PROCESS_ROLES:
            raise ConfigurationError("Invalid V2 process role.")
        try:
            url = make_url(self.database_url)
        except Exception as exc:
            raise ConfigurationError("NEMSEI_V2_DATABASE_URL must be a PostgreSQL URL.") from exc
        if url.drivername not in {"postgresql", "postgresql+psycopg"} or not url.host or not url.database or not url.username:
            raise ConfigurationError("NEMSEI_V2_DATABASE_URL must be a complete PostgreSQL URL.")
        if self.environment in {"preview", "production"} and (not self.secret_key or self.secret_key.lower() in INSECURE_SECRET_KEYS):
            raise ConfigurationError("NEMSEI_V2_SECRET_KEY must be non-default outside development/test.")
        if require_auth and (not self.admin_username or self.admin_password_hash.count("$") < 2):
            raise ConfigurationError(
                "V2 administrator credentials are required. A password hash missing its "
                "`$` sections usually means Compose interpolated the env file: every "
                "literal `$` in .env.v2 must be written `$$`."
            )
        if self.device_status_poll_interval_minutes <= 0:
            raise ConfigurationError("Device status poll interval must be positive.")
        if self.device_status_poll_enabled and self.device_status_poll_connection_id is None:
            raise ConfigurationError("Device status polling requires an explicit connection id; there is no portfolio-wide mode.")
        if self.device_status_poll_enabled and (
            self.device_status_poll_max_cycles is None or self.device_status_poll_max_cycles <= 0
        ):
            raise ConfigurationError("Device status polling requires a positive lifetime cycle cap; there is no uncapped mode.")
        if self.production_sync_scheduler_interval_hours <= 0:
            raise ConfigurationError("Production sync scheduler interval must be positive.")
        if self.production_sync_scheduler_enabled and self.production_sync_scheduler_connection_id is None:
            raise ConfigurationError("Production sync scheduling requires an explicit connection id; there is no portfolio-wide mode.")
        if self.diagnostic_incident_evaluation_interval_minutes <= 0:
            raise ConfigurationError("Diagnostic incident evaluation interval must be positive.")
        if self.notification_processing_interval_minutes <= 0:
            raise ConfigurationError("Notification processing interval must be positive.")
        if self.digest_generation_interval_minutes <= 0:
            raise ConfigurationError("Digest generation interval must be positive.")
        if min(self.worker_poll_seconds, self.worker_lease_seconds, self.scheduler_lease_seconds, self.db_pool_size, self.db_statement_timeout_ms, self.db_lock_timeout_ms, self.db_idle_transaction_timeout_ms, self.db_pool_recycle_seconds, self.production_max_source_days, self.production_reconciliation_max_source_days, self.production_backfill_max_source_days, self.production_backfill_chunk_days) <= 0 or self.db_max_overflow < 0:
            raise ConfigurationError("V2 timing and pool settings must be positive.")
        if self.production_backfill_chunk_days > self.production_backfill_max_source_days:
            raise ConfigurationError("V2 production backfill chunk cannot exceed its bounded window limit.")
        return self
