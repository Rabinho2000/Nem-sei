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
