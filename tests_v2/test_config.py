from __future__ import annotations

import dataclasses

import pytest

from nemsei.config import ConfigurationError, Settings


def configured(url: str = "postgresql+psycopg://user:secret@localhost:5432/nemsei_v2_test") -> Settings:
    return Settings(environment="test", database_url=url, secret_key="test-secret", admin_username="admin", admin_password_hash="hash", capabilities={"provider_reads": False, "provider_mutations": False, "notifications": False, "report_distribution": False}, testing=True)


@pytest.mark.parametrize("url", ["mysql://user:secret@localhost/v2", "postgresql+psycopg://user:secret@/db", ""])
def test_v2_requires_complete_postgres_url(url: str) -> None:
    with pytest.raises(ConfigurationError):
        configured(url).validate()


def test_database_url_can_come_from_a_mounted_secret_file(monkeypatch, tmp_path) -> None:
    secret = tmp_path / "database_url"
    secret.write_text("postgresql+psycopg://nemsei:secret@postgres:5432/nemsei_v2\n", encoding="utf-8")
    monkeypatch.delenv("NEMSEI_V2_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL_FILE", str(secret))
    configured_from_env = Settings.from_environment()
    assert configured_from_env.database_url == "postgresql+psycopg://nemsei:secret@postgres:5432/nemsei_v2"


def test_database_defaults_are_role_specific(monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", "postgresql+psycopg://nemsei:secret@postgres:5432/nemsei_v2")
    monkeypatch.setenv("NEMSEI_V2_PROCESS_ROLE", "worker")
    worker = Settings.from_environment()
    monkeypatch.setenv("NEMSEI_V2_PROCESS_ROLE", "scheduler")
    scheduler = Settings.from_environment()
    assert worker.db_statement_timeout_ms > scheduler.db_statement_timeout_ms
    assert worker.db_idle_transaction_timeout_ms > scheduler.db_idle_transaction_timeout_ms


def test_v2_configuration_defaults_all_capabilities_to_denied() -> None:
    assert configured().validate().capabilities == {"provider_reads": False, "provider_mutations": False, "notifications": False, "report_distribution": False}


def test_device_status_poll_disabled_by_default_needs_no_extra_config() -> None:
    # The default, off, must validate cleanly with connection_id/max_cycles left at None --
    # nothing about turning the feature off should require configuring it.
    assert configured().validate().device_status_poll_enabled is False


def test_device_status_poll_enabled_requires_an_explicit_connection_id() -> None:
    """M7 Fatia 3: no 'poll every FusionSolar connection' mode exists structurally."""
    settings = dataclasses.replace(configured(), device_status_poll_enabled=True, device_status_poll_max_cycles=10)
    with pytest.raises(ConfigurationError):
        settings.validate()


def test_device_status_poll_enabled_requires_a_positive_lifetime_cycle_cap() -> None:
    """An unattended run can never be enabled uncapped, even by omission."""
    settings = dataclasses.replace(configured(), device_status_poll_enabled=True, device_status_poll_connection_id=3)
    with pytest.raises(ConfigurationError):
        settings.validate()

    zero_cap = dataclasses.replace(settings, device_status_poll_max_cycles=0)
    with pytest.raises(ConfigurationError):
        zero_cap.validate()


def test_device_status_poll_enabled_with_connection_id_and_cap_validates() -> None:
    settings = dataclasses.replace(
        configured(),
        device_status_poll_enabled=True,
        device_status_poll_connection_id=3,
        device_status_poll_max_cycles=48,
    )
    validated = settings.validate()
    assert validated.device_status_poll_connection_id == 3
    assert validated.device_status_poll_max_cycles == 48


def test_device_status_poll_settings_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", "postgresql+psycopg://nemsei:secret@postgres:5432/nemsei_v2")
    monkeypatch.setenv("NEMSEI_V2_DEVICE_STATUS_POLL_ENABLED", "true")
    monkeypatch.setenv("NEMSEI_V2_DEVICE_STATUS_POLL_CONNECTION_ID", "3")
    monkeypatch.setenv("NEMSEI_V2_DEVICE_STATUS_POLL_INTERVAL_MINUTES", "30")
    monkeypatch.setenv("NEMSEI_V2_DEVICE_STATUS_POLL_MAX_CYCLES", "48")
    settings = Settings.from_environment()
    assert settings.device_status_poll_enabled is True
    assert settings.device_status_poll_connection_id == 3
    assert settings.device_status_poll_max_cycles == 48


def test_production_sync_scheduler_disabled_by_default_needs_no_extra_config() -> None:
    assert configured().validate().production_sync_scheduler_enabled is False


def test_production_sync_scheduler_enabled_requires_an_explicit_connection_id() -> None:
    """Same structural restraint as device-status polling: no 'sync every
    FusionSolar connection' mode exists -- a shared, rate-limited account
    needs a deliberate second call site to scale, not a config flip."""
    settings = dataclasses.replace(configured(), production_sync_scheduler_enabled=True)
    with pytest.raises(ConfigurationError):
        settings.validate()


def test_production_sync_scheduler_enabled_with_connection_id_validates() -> None:
    settings = dataclasses.replace(
        configured(),
        production_sync_scheduler_enabled=True,
        production_sync_scheduler_connection_id=3,
    )
    validated = settings.validate()
    assert validated.production_sync_scheduler_connection_id == 3
    assert validated.production_sync_scheduler_interval_hours == 24


def test_production_sync_scheduler_settings_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", "postgresql+psycopg://nemsei:secret@postgres:5432/nemsei_v2")
    monkeypatch.setenv("NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_CONNECTION_ID", "3")
    monkeypatch.setenv("NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_INTERVAL_HOURS", "12")
    settings = Settings.from_environment()
    assert settings.production_sync_scheduler_enabled is True
    assert settings.production_sync_scheduler_connection_id == 3
    assert settings.production_sync_scheduler_interval_hours == 12


# --- Telegram O&M redesign, Fatia 4: morning briefing settings -----------------


def test_morning_briefing_rejects_an_unknown_timezone() -> None:
    settings = dataclasses.replace(configured(), morning_briefing_timezone="Not/A_Real_Zone")
    with pytest.raises(ConfigurationError):
        settings.validate()


def test_morning_briefing_accepts_a_real_timezone() -> None:
    settings = dataclasses.replace(configured(), morning_briefing_timezone="Europe/Lisbon")
    assert settings.validate().morning_briefing_timezone == "Europe/Lisbon"


@pytest.mark.parametrize("hour,minute", [(-1, 0), (24, 0), (9, -1), (9, 60)])
def test_morning_briefing_rejects_an_invalid_time_of_day(hour: int, minute: int) -> None:
    settings = dataclasses.replace(configured(), morning_briefing_hour=hour, morning_briefing_minute=minute)
    with pytest.raises(ConfigurationError):
        settings.validate()


def test_morning_briefing_settings_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", "postgresql+psycopg://nemsei:secret@postgres:5432/nemsei_v2")
    monkeypatch.setenv("NEMSEI_V2_MORNING_BRIEFING_ENABLED", "true")
    monkeypatch.setenv("NEMSEI_V2_MORNING_BRIEFING_HOUR", "9")
    monkeypatch.setenv("NEMSEI_V2_MORNING_BRIEFING_MINUTE", "0")
    monkeypatch.setenv("NEMSEI_V2_MORNING_BRIEFING_TIMEZONE", "Europe/Lisbon")
    settings = Settings.from_environment()
    assert settings.morning_briefing_enabled is True
    assert settings.morning_briefing_hour == 9
    assert settings.morning_briefing_timezone == "Europe/Lisbon"


def test_recovery_digest_interval_must_be_positive() -> None:
    settings = dataclasses.replace(configured(), recovery_digest_interval_minutes=0)
    with pytest.raises(ConfigurationError):
        settings.validate()
