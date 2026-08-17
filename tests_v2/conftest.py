from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from werkzeug.security import generate_password_hash

from nemsei.config import Settings


@pytest.fixture
def settings() -> Settings:
    base = os.environ.get("NEMSEI_V2_TEST_DATABASE_URL", "postgresql+psycopg://nemsei:nemsei-test@127.0.0.1:55432/nemsei_v2_test")
    url = make_url(base)
    name = f"nemsei_v2_test_{uuid.uuid4().hex}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield Settings(
            environment="test",
            database_url=url.set(database=name).render_as_string(hide_password=False),
            secret_key="test-secret",
            admin_username="admin",
            admin_password_hash=generate_password_hash("correct-password"),
            capabilities={"provider_reads": False, "provider_mutations": False, "notifications": False, "report_distribution": False},
            testing=True,
        )
    finally:
        with admin.connect() as connection:
            connection.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name AND pid <> pg_backend_pid()"), {"name": name})
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
