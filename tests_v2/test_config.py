from __future__ import annotations

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
