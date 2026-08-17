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
            raise ConfigurationError("V2 administrator credentials are required.")
        if min(self.worker_poll_seconds, self.worker_lease_seconds, self.scheduler_lease_seconds, self.db_pool_size, self.db_statement_timeout_ms, self.db_lock_timeout_ms, self.db_idle_transaction_timeout_ms, self.db_pool_recycle_seconds, self.production_max_source_days, self.production_reconciliation_max_source_days, self.production_backfill_max_source_days, self.production_backfill_chunk_days) <= 0 or self.db_max_overflow < 0:
            raise ConfigurationError("V2 timing and pool settings must be positive.")
        if self.production_backfill_chunk_days > self.production_backfill_max_source_days:
            raise ConfigurationError("V2 production backfill chunk cannot exceed its bounded window limit.")
        return self
