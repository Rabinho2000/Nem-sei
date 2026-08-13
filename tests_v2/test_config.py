from __future__ import annotations

from pathlib import Path

import pytest

from nemsei.config import ConfigurationError, Settings, V2_DATABASE_FILENAME


def settings(tmp_path: Path, *, database_path: Path | None = None) -> Settings:
    root = tmp_path / "v2-data"
    database = database_path or root / V2_DATABASE_FILENAME
    return Settings(
        environment="test",
        data_root=root,
        database_url=f"sqlite:///{database}",
        database_path=database,
        secret_key="test-secret",
        admin_username="admin",
        admin_password_hash="hash",
        capabilities={
            "provider_reads": False,
            "provider_mutations": False,
            "notifications": False,
            "report_distribution": False,
        },
        testing=True,
    )


def test_v2_database_must_use_the_reserved_filename(tmp_path: Path) -> None:
    configured = settings(tmp_path, database_path=tmp_path / "v2-data" / "monitoring_board.db")
    with pytest.raises(ConfigurationError, match="filename"):
        configured.validate()


def test_v2_database_must_be_inside_its_data_root(tmp_path: Path) -> None:
    configured = settings(tmp_path, database_path=tmp_path / "outside" / V2_DATABASE_FILENAME)
    with pytest.raises(ConfigurationError, match="inside"):
        configured.validate()


def test_v2_configuration_defaults_all_capabilities_to_denied(tmp_path: Path) -> None:
    configured = settings(tmp_path).validate()
    assert configured.capabilities == {
        "provider_reads": False,
        "provider_mutations": False,
        "notifications": False,
        "report_distribution": False,
    }


def test_database_url_cannot_bypass_validated_database_path(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    unsafe = Settings(
        **{**configured.__dict__, "database_url": f"sqlite:///{tmp_path / 'outside' / V2_DATABASE_FILENAME}"}
    )
    with pytest.raises(ConfigurationError, match="must match"):
        unsafe.validate()


@pytest.mark.parametrize("secret", ["changeme", "change-me", "secret", "development", "default"])
def test_preview_rejects_known_insecure_secret_keys(tmp_path: Path, secret: str) -> None:
    configured = settings(tmp_path)
    preview = Settings(**{**configured.__dict__, "environment": "preview", "secret_key": secret, "testing": False})
    with pytest.raises(ConfigurationError, match="non-default"):
        preview.validate()


def test_preview_rejects_test_mode(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    preview = Settings(**{**configured.__dict__, "environment": "preview", "secret_key": "a-safe-secret", "testing": True})
    with pytest.raises(ConfigurationError, match="TESTING"):
        preview.validate()
