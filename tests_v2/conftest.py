from __future__ import annotations

from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from nemsei.config import Settings, V2_DATABASE_FILENAME


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = tmp_path / "v2-data"
    return Settings(
        environment="test",
        data_root=root,
        database_url=f"sqlite:///{root / V2_DATABASE_FILENAME}",
        database_path=root / V2_DATABASE_FILENAME,
        secret_key="test-secret",
        admin_username="admin",
        admin_password_hash=generate_password_hash("correct-password"),
        capabilities={
            "provider_reads": False,
            "provider_mutations": False,
            "notifications": False,
            "report_distribution": False,
        },
        testing=True,
    )
