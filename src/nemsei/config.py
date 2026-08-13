"""Validated V2 runtime settings with strict data-root isolation."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


VALID_ENVIRONMENTS = {"development", "test", "preview", "production"}
V2_DATABASE_FILENAME = "nemsei_v2.db"
CAPABILITIES = (
    "provider_reads",
    "provider_mutations",
    "notifications",
    "report_distribution",
)


class ConfigurationError(ValueError):
    """Raised before a V2 process can use an unsafe configuration."""


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Expected a boolean value, got {value!r}.")


def sqlite_path_from_url(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ConfigurationError("NEMSEI_V2_DATABASE_URL must be a SQLite file URL.")
    raw_path = unquote(database_url[len(prefix) :])
    if not raw_path or raw_path == ":memory:" or "?" in raw_path:
        raise ConfigurationError("V2 requires a SQLite database file, not memory or query URLs.")
    return Path(raw_path).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    environment: str
    data_root: Path
    database_url: str
    database_path: Path
    secret_key: str
    admin_username: str
    admin_password_hash: str
    capabilities: dict[str, bool]
    worker_poll_seconds: float = 2.0
    worker_lease_seconds: int = 30
    scheduler_lease_seconds: int = 30
    testing: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        environment = os.environ.get("NEMSEI_V2_ENV", "development").strip().lower()
        data_root = Path(
            os.environ.get("NEMSEI_V2_DATA_ROOT", "./data-v2")
        ).expanduser().resolve()
        database_url = os.environ.get(
            "NEMSEI_V2_DATABASE_URL",
            f"sqlite:///{data_root / V2_DATABASE_FILENAME}",
        )
        database_path = sqlite_path_from_url(database_url)
        capabilities = {
            capability: parse_bool(
                os.environ.get(f"NEMSEI_V2_{capability.upper()}"), default=False
            )
            for capability in CAPABILITIES
        }
        return cls(
            environment=environment,
            data_root=data_root,
            database_url=database_url,
            database_path=database_path,
            secret_key=os.environ.get("NEMSEI_V2_SECRET_KEY", ""),
            admin_username=os.environ.get("NEMSEI_V2_ADMIN_USERNAME", "admin").strip(),
            admin_password_hash=os.environ.get("NEMSEI_V2_ADMIN_PASSWORD_HASH", "").strip(),
            capabilities=capabilities,
            worker_poll_seconds=float(os.environ.get("NEMSEI_V2_WORKER_POLL_SECONDS", "2")),
            worker_lease_seconds=int(os.environ.get("NEMSEI_V2_WORKER_LEASE_SECONDS", "30")),
            scheduler_lease_seconds=int(os.environ.get("NEMSEI_V2_SCHEDULER_LEASE_SECONDS", "30")),
            testing=parse_bool(os.environ.get("NEMSEI_V2_TESTING"), default=False),
        )

    def validate(self, *, require_auth: bool = False) -> "Settings":
        if self.environment not in VALID_ENVIRONMENTS:
            raise ConfigurationError("NEMSEI_V2_ENV must be development, test, preview, or production.")
        resolved_root = self.data_root.resolve()
        resolved_database = self.database_path.resolve()
        if resolved_database.name != V2_DATABASE_FILENAME:
            raise ConfigurationError(f"V2 database filename must be {V2_DATABASE_FILENAME}.")
        try:
            resolved_database.relative_to(resolved_root)
        except ValueError as exc:
            raise ConfigurationError("V2 database must be inside NEMSEI_V2_DATA_ROOT.") from exc
        if self.environment in {"preview", "production"} and not self.secret_key:
            raise ConfigurationError("NEMSEI_V2_SECRET_KEY is required outside development/test.")
        if require_auth and (not self.admin_username or not self.admin_password_hash):
            raise ConfigurationError("V2 administrator username and password hash are required.")
        if self.worker_poll_seconds <= 0 or self.worker_lease_seconds <= 0 or self.scheduler_lease_seconds <= 0:
            raise ConfigurationError("Worker and scheduler timings must be positive.")
        return self
