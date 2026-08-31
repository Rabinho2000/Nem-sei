"""Validated V2 PostgreSQL runtime settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url


VALID_ENVIRONMENTS = {"development", "test", "preview", "production"}
PROCESS_ROLES = {"web", "scheduler", "worker", "migrate", "admin", "scada_listener"}
CAPABILITIES = ("provider_reads", "provider_mutations", "notifications", "report_distribution")
INSECURE_SECRET_KEYS = {"changeme", "change-me", "secret", "development", "default"}
ROLE_DATABASE_DEFAULTS = {
    "web": {"pool_size": 2, "max_overflow": 1, "statement_timeout_ms": 15000, "lock_timeout_ms": 3000, "idle_transaction_timeout_ms": 30000},
    "scheduler": {"pool_size": 1, "max_overflow": 1, "statement_timeout_ms": 10000, "lock_timeout_ms": 3000, "idle_transaction_timeout_ms": 20000},
    "worker": {"pool_size": 2, "max_overflow": 1, "statement_timeout_ms": 30000, "lock_timeout_ms": 5000, "idle_transaction_timeout_ms": 60000},
    "migrate": {"pool_size": 1, "max_overflow": 0, "statement_timeout_ms": 120000, "lock_timeout_ms": 10000, "idle_transaction_timeout_ms": 120000},
    "admin": {"pool_size": 1, "max_overflow": 0, "statement_timeout_ms": 60000, "lock_timeout_ms": 10000, "idle_transaction_timeout_ms": 60000},
    # One short transaction per poll, one thread per dialled-in dongle, so
    # this role needs breadth rather than duration: a wider pool than the
    # others and the tightest idle-transaction ceiling of any of them, since
    # a session held open across a poll interval would pin a connection for
    # the lifetime of a logger.
    "scada_listener": {"pool_size": 5, "max_overflow": 10, "statement_timeout_ms": 10000, "lock_timeout_ms": 3000, "idle_transaction_timeout_ms": 15000},
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


def external_capability_enabled(capability: str) -> bool:
    """Whether one `CAPABILITIES` switch is on, read from the process environment.

    `Settings.capabilities` says the same thing, and where a `Settings` is
    already in hand it is the better source. This exists for the few places
    that decide whether *this process* may touch the network and that are
    reached without one -- notably
    `notifications/telegram_client.default_client_factory`, which already reads
    the bot token from the environment for the same reason.

    Default-deny, matching `safety/external_actions.py`: an unset switch is
    off, never on.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"Unknown external capability: {capability}")
    return parse_bool(os.environ.get(f"NEMSEI_V2_{capability.upper()}"), default=False)


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
    # How many source days one incremental run asks the provider for, and how
    # long it waits before asking for the next chunk.
    #
    # The incremental sync used to request its whole outstanding window in one
    # run: one provider call per day, back to back. On 2026-08-30 that was 31
    # calls in sixteen seconds against a shared account whose brake is on
    # *frequency*; it was refused on the thirtieth. Because the run then ended
    # `partial`, the cursor -- which only advances over a fully complete window
    # -- stayed where it was, so the next day's window was a day wider and
    # failed a day sooner. It had failed every day since 2026-08-24 and was one
    # day away from exceeding `production_max_source_days` entirely.
    #
    # Chunking breaks that loop without touching a single guard: each chunk is
    # a window small enough to finish, so it completes, so the cursor advances
    # by exactly the days that were actually covered, and the next chunk starts
    # from there. Seven is well under the ~29 consecutive calls the account has
    # been seen to tolerate, and the pause between chunks is what keeps a
    # catch-up from becoming the same burst spread over more runs.
    production_incremental_chunk_days: int = 7
    production_incremental_chunk_pause_seconds: int = 300
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
    # Plant-state reads: "is this installation up right now". One switch per
    # provider account, the same restraint the production settings above
    # follow and for the same reason -- but on a minutes interval rather than
    # hours, because this is the read the outage alerting depends on and a
    # plant that fell an hour ago is news that arrived an hour late. Cheap
    # enough to afford it: FusionSolar answers 100 plants per call.
    current_monitoring_scheduler_enabled: bool = False
    current_monitoring_scheduler_interval_minutes: int = 15
    current_monitoring_scheduler_connection_id: int | None = None
    sigenergy_current_monitoring_scheduler_enabled: bool = False
    sigenergy_current_monitoring_scheduler_interval_minutes: int = 15
    sigenergy_current_monitoring_scheduler_connection_id: int | None = None
    # M7 Fatia 5 / D1 (docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md): the
    # periodic diagnostic-incident evaluator. Off by default, same pattern as
    # every other recurring behaviour this codebase adds -- but unlike
    # device-status polling this makes zero provider calls (it only reads
    # already-persisted device_status_facts and writes diagnostic_incidents),
    # so it needs neither a connection id nor a call-budget hard cap; it runs
    # across every asset that owns a device, by design.
    diagnostic_incident_evaluation_enabled: bool = False
    diagnostic_incident_evaluation_interval_minutes: int = 15
    # Closing sync runs whose owner died. On by default, unlike every other
    # schedule here, and deliberately: this is crash recovery, not a capability.
    # It makes no provider call and touches nothing but rows that are already
    # unreachable -- a run nobody can finish. Leaving it off would mean the
    # default deployment keeps accumulating rows that say `running` forever,
    # which is the state this exists to end. The switch is here for a
    # deployment that wants to inspect such rows by hand first.
    sync_run_sweep_enabled: bool = True
    sync_run_sweep_interval_minutes: int = 15
    # How long a run may show no evidence of life before it is classified as
    # abandoned. See `sync/abandonment.py` for why an hour is generous rather
    # than arbitrary.
    sync_run_sweep_silence_grace_minutes: int = 60
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
    # Huawei SCADA (docs/v2/HUAWEI_SCADA.md). Unlike every other integration
    # in this codebase, this one is *inbound*: the logger dials in and the
    # listener answers. So there is no interval to schedule and no call budget
    # to protect -- what these settings bound is a socket, not a provider.
    #
    # Off by default and restricted to one explicit connection, the same
    # restraint the other integrations use, and for a sharper reason here: two
    # listeners pointed at one connection would double every sample, with no
    # provider-side collision to make the mistake visible.
    #
    # The host defaults to every interface *inside the container*. Which
    # address and which public port actually reach it is a Compose port
    # publication and a NAT rule -- deliberately not a code constant, so a
    # pilot's network can change without a release.
    huawei_scada_listener_enabled: bool = False
    huawei_scada_listener_host: str = "0.0.0.0"
    huawei_scada_listener_port: int = 1502
    huawei_scada_listener_connection_id: int | None = None
    # The poll interval doubles as the keep-alive, which is why `validate()`
    # requires it to be shorter than the idle timeout: a NAT mapping in front
    # of the customer's router expires on silence.
    huawei_scada_poll_interval_seconds: int = 30
    huawei_scada_read_timeout_seconds: int = 15
    huawei_scada_handshake_timeout_seconds: int = 60
    huawei_scada_idle_timeout_seconds: int = 300
    huawei_scada_max_sessions: int = 32
    # The rollup is an ordinary durable job, in the worker, over rows the
    # database already holds -- zero provider calls. It re-integrates the last
    # `lookback_days` so a day integrated while still in progress is corrected
    # once complete, which the append-only revision rule turns into a
    # supersession rather than a duplicate.
    huawei_scada_rollup_enabled: bool = False
    huawei_scada_rollup_interval_minutes: int = 60
    huawei_scada_rollup_connection_id: int | None = None
    huawei_scada_rollup_lookback_days: int = 2
    # Retention exists because a 30-second cadence writes ~2 880 rows per
    # plant per day. It only ever deletes samples whose day already carries a
    # complete production fact, so deleting cannot erase evidence that has not
    # been used yet.
    huawei_scada_retention_enabled: bool = False
    huawei_scada_retention_interval_minutes: int = 1440
    huawei_scada_retention_days: int = 90
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
            production_incremental_chunk_days=int(os.environ.get("NEMSEI_V2_PRODUCTION_INCREMENTAL_CHUNK_DAYS", "7")),
            production_incremental_chunk_pause_seconds=int(os.environ.get("NEMSEI_V2_PRODUCTION_INCREMENTAL_CHUNK_PAUSE_SECONDS", "300")),
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
            current_monitoring_scheduler_enabled=parse_bool(os.environ.get("NEMSEI_V2_CURRENT_MONITORING_SCHEDULER_ENABLED"), default=False),
            current_monitoring_scheduler_interval_minutes=int(os.environ.get("NEMSEI_V2_CURRENT_MONITORING_SCHEDULER_INTERVAL_MINUTES", "15")),
            current_monitoring_scheduler_connection_id=_optional_int(os.environ.get("NEMSEI_V2_CURRENT_MONITORING_SCHEDULER_CONNECTION_ID")),
            sigenergy_current_monitoring_scheduler_enabled=parse_bool(os.environ.get("NEMSEI_V2_SIGENERGY_CURRENT_MONITORING_SCHEDULER_ENABLED"), default=False),
            sigenergy_current_monitoring_scheduler_interval_minutes=int(os.environ.get("NEMSEI_V2_SIGENERGY_CURRENT_MONITORING_SCHEDULER_INTERVAL_MINUTES", "15")),
            sigenergy_current_monitoring_scheduler_connection_id=_optional_int(os.environ.get("NEMSEI_V2_SIGENERGY_CURRENT_MONITORING_SCHEDULER_CONNECTION_ID")),
            sync_run_sweep_enabled=parse_bool(os.environ.get("NEMSEI_V2_SYNC_RUN_SWEEP_ENABLED"), default=True),
            sync_run_sweep_interval_minutes=int(os.environ.get("NEMSEI_V2_SYNC_RUN_SWEEP_INTERVAL_MINUTES", "15")),
            sync_run_sweep_silence_grace_minutes=int(os.environ.get("NEMSEI_V2_SYNC_RUN_SWEEP_SILENCE_GRACE_MINUTES", "60")),
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
            huawei_scada_listener_enabled=parse_bool(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_LISTENER_ENABLED"), default=False
            ),
            huawei_scada_listener_host=os.environ.get("NEMSEI_V2_HUAWEI_SCADA_LISTENER_HOST", "0.0.0.0").strip(),
            huawei_scada_listener_port=int(os.environ.get("NEMSEI_V2_HUAWEI_SCADA_LISTENER_PORT", "1502")),
            huawei_scada_listener_connection_id=_optional_int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_LISTENER_CONNECTION_ID")
            ),
            huawei_scada_poll_interval_seconds=int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_POLL_INTERVAL_SECONDS", "30")
            ),
            huawei_scada_read_timeout_seconds=int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_READ_TIMEOUT_SECONDS", "15")
            ),
            huawei_scada_handshake_timeout_seconds=int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_HANDSHAKE_TIMEOUT_SECONDS", "60")
            ),
            huawei_scada_idle_timeout_seconds=int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_IDLE_TIMEOUT_SECONDS", "300")
            ),
            huawei_scada_max_sessions=int(os.environ.get("NEMSEI_V2_HUAWEI_SCADA_MAX_SESSIONS", "32")),
            huawei_scada_rollup_enabled=parse_bool(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_ROLLUP_ENABLED"), default=False
            ),
            huawei_scada_rollup_interval_minutes=int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_ROLLUP_INTERVAL_MINUTES", "60")
            ),
            huawei_scada_rollup_connection_id=_optional_int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_ROLLUP_CONNECTION_ID")
            ),
            huawei_scada_rollup_lookback_days=int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_ROLLUP_LOOKBACK_DAYS", "2")
            ),
            huawei_scada_retention_enabled=parse_bool(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_RETENTION_ENABLED"), default=False
            ),
            huawei_scada_retention_interval_minutes=int(
                os.environ.get("NEMSEI_V2_HUAWEI_SCADA_RETENTION_INTERVAL_MINUTES", "1440")
            ),
            huawei_scada_retention_days=int(os.environ.get("NEMSEI_V2_HUAWEI_SCADA_RETENTION_DAYS", "90")),
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
        for label, interval, enabled, connection_id in (
            ("FusionSolar", self.current_monitoring_scheduler_interval_minutes, self.current_monitoring_scheduler_enabled, self.current_monitoring_scheduler_connection_id),
            ("Sigenergy", self.sigenergy_current_monitoring_scheduler_interval_minutes, self.sigenergy_current_monitoring_scheduler_enabled, self.sigenergy_current_monitoring_scheduler_connection_id),
        ):
            if interval <= 0:
                raise ConfigurationError(f"{label} current monitoring interval must be positive.")
            if enabled and connection_id is None:
                raise ConfigurationError(f"{label} current monitoring requires an explicit connection id; there is no portfolio-wide mode.")
        if self.diagnostic_incident_evaluation_interval_minutes <= 0:
            raise ConfigurationError("Diagnostic incident evaluation interval must be positive.")
        if self.notification_processing_interval_minutes <= 0:
            raise ConfigurationError("Notification processing interval must be positive.")
        if self.digest_generation_interval_minutes <= 0:
            raise ConfigurationError("Digest generation interval must be positive.")
        if not 1 <= self.huawei_scada_listener_port <= 65535:
            raise ConfigurationError("Huawei SCADA listener port must be a valid TCP port.")
        if min(
            self.huawei_scada_poll_interval_seconds,
            self.huawei_scada_read_timeout_seconds,
            self.huawei_scada_handshake_timeout_seconds,
            self.huawei_scada_idle_timeout_seconds,
            self.huawei_scada_max_sessions,
            self.huawei_scada_rollup_interval_minutes,
            self.huawei_scada_rollup_lookback_days,
            self.huawei_scada_retention_interval_minutes,
            self.huawei_scada_retention_days,
        ) <= 0:
            raise ConfigurationError("Huawei SCADA timing and session settings must be positive.")
        if self.huawei_scada_listener_enabled and self.huawei_scada_listener_connection_id is None:
            raise ConfigurationError(
                "The Huawei SCADA listener requires an explicit connection id; there is no portfolio-wide mode."
            )
        # The poll is the keep-alive. A poll slower than the idle timeout means
        # the listener would tear down a healthy session while waiting for its
        # own next request.
        if self.huawei_scada_poll_interval_seconds >= self.huawei_scada_idle_timeout_seconds:
            raise ConfigurationError("Huawei SCADA poll interval must be shorter than the idle timeout.")
        if self.huawei_scada_read_timeout_seconds > self.huawei_scada_poll_interval_seconds:
            raise ConfigurationError("Huawei SCADA read timeout cannot exceed the poll interval.")
        if self.huawei_scada_rollup_enabled and self.huawei_scada_rollup_connection_id is None:
            raise ConfigurationError(
                "Huawei SCADA rollup requires an explicit connection id; there is no portfolio-wide mode."
            )
        # Retention must never overtake the window the rollup still re-reads,
        # or a day would lose its samples before its energy was final.
        if self.huawei_scada_retention_days <= self.huawei_scada_rollup_lookback_days:
            raise ConfigurationError("Huawei SCADA retention must outlast the rollup lookback window.")
        if min(self.worker_poll_seconds, self.worker_lease_seconds, self.scheduler_lease_seconds, self.db_pool_size, self.db_statement_timeout_ms, self.db_lock_timeout_ms, self.db_idle_transaction_timeout_ms, self.db_pool_recycle_seconds, self.production_max_source_days, self.production_reconciliation_max_source_days, self.production_backfill_max_source_days, self.production_backfill_chunk_days) <= 0 or self.db_max_overflow < 0:
            raise ConfigurationError("V2 timing and pool settings must be positive.")
        if self.production_backfill_chunk_days > self.production_backfill_max_source_days:
            raise ConfigurationError("V2 production backfill chunk cannot exceed its bounded window limit.")
        if self.sync_run_sweep_interval_minutes <= 0 or self.sync_run_sweep_silence_grace_minutes <= 0:
            raise ConfigurationError("Sync run sweep interval and silence grace must be positive.")
        if self.production_incremental_chunk_days <= 0 or self.production_incremental_chunk_pause_seconds < 0:
            raise ConfigurationError("V2 production incremental chunking must be positive; there is no unchunked mode.")
        # A chunk larger than the window cap would let one run ask for more than
        # the cap allows, which is the safety limit the cap exists to be.
        if self.production_incremental_chunk_days > self.production_max_source_days:
            raise ConfigurationError("V2 production incremental chunk cannot exceed the normal-sync window limit.")
        return self
