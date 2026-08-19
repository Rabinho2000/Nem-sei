from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from nemsei.config import ConfigurationError, Settings


ROOT = Path(__file__).parents[1]
COMPOSE_UP = ROOT / "scripts/v2_compose_up.sh"
BACKUP = ROOT / "scripts/v2_postgres_backup.sh"


def head_resolver():
    """Load the deployment helper, which lives in scripts/ rather than a package."""
    path = ROOT / "scripts/v2_resolve_alembic_head.py"
    spec = importlib.util.spec_from_file_location("v2_resolve_alembic_head", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_compose_uses_private_pinned_postgres() -> None:
    compose = (ROOT / "docker-compose.v2.yml").read_text(encoding="utf-8")
    assert "postgres:16.11-bookworm@sha256:" in compose
    assert "nemsei-v2-postgres-data:/var/lib/postgresql/data" in compose
    assert "POSTGRES_PASSWORD_FILE" in compose
    assert "v2_database_url" in compose
    assert "postgres:" in compose
    assert "ports:" not in compose.split("  migrate:", 1)[0]
    assert "127.0.0.1:${NEMSEI_V2_WEB_PORT:-5002}:5000" in compose
    assert "Nem-sei/data" not in compose


def test_canonical_deployment_validates_before_starting_roles() -> None:
    script = (ROOT / "scripts/v2_compose_up.sh").read_text(encoding="utf-8")
    preflight = script.index("verify_v2_runtime_isolation.py")
    postgres = script.index("up -d postgres")
    migrate = script.index('run --rm migrate')
    startup = script.index("up -d web scheduler worker")
    assert preflight < postgres < migrate < startup
    assert "config --format json" in script
    assert "NEMSEI_V2_WORKER_SCALE" in script
    assert "accepts no Compose scale arguments" in script


def test_deployment_rebuilds_the_profiled_migrate_image_before_migrating() -> None:
    # `migrate` sits behind the `manual` profile, so a plain `compose build`
    # skips it and a stale image can migrate to its own older head.
    script = COMPOSE_UP.read_text(encoding="utf-8")
    build = script.index("--profile manual build migrate")
    migrate = script.index("run --rm migrate")
    startup = script.index("up -d web scheduler worker")
    assert build < migrate < startup


def test_deployment_verifies_the_migrated_revision_before_serving_traffic() -> None:
    script = COMPOSE_UP.read_text(encoding="utf-8")
    migrate = script.index("run --rm migrate")
    check = script.index("--live-revision")
    startup = script.index("up -d web scheduler worker")
    assert migrate < check < startup
    assert "v2_resolve_alembic_head.py" in script
    assert "SELECT version_num FROM alembic_version" in script


@pytest.mark.parametrize("script_path", [COMPOSE_UP, BACKUP, ROOT / "scripts/v2_postgres_restore_smoke.sh"])
def test_deployment_scripts_never_hardcode_an_alembic_revision(script_path: Path) -> None:
    # Revision names must always be resolved from the checked-out graph.
    assert not re.search(r"\b\d{4}_[a-z0-9_]+\b", script_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("script_path", [COMPOSE_UP, BACKUP, ROOT / "scripts/v2_postgres_restore_smoke.sh"])
def test_deployment_scripts_are_valid_shell(script_path: Path) -> None:
    assert subprocess.run(["bash", "-n", str(script_path)], capture_output=True).returncode == 0


def test_migrated_revision_must_equal_the_resolved_head() -> None:
    module = head_resolver()
    module.validate_live_revision("0009_example", "0009_example")
    with pytest.raises(module.AlembicHeadError, match="did not run"):
        module.validate_live_revision("", "0009_example")
    with pytest.raises(module.AlembicHeadError, match="stale"):
        module.validate_live_revision("0008_previous", "0009_example")


def test_repository_head_is_resolved_dynamically_and_is_single() -> None:
    module = head_resolver()
    head = module.resolve_single_head(ROOT / "alembic.ini")
    assert head and head not in {"head", "heads"}
    revisions = {path.stem for path in (ROOT / "migrations/versions").glob("[0-9]*.py")}
    assert head in revisions


def test_backup_archives_are_created_restricted_not_tightened_afterwards(tmp_path: Path) -> None:
    script = BACKUP.read_text(encoding="utf-8")
    assert "umask 077" in script
    assert script.index("umask 077") < script.index("pg_dump")
    assert "stat -c '%a'" in script and "600" in script
    assert "chmod" not in script

    # The umask contract itself: a redirect must not produce a readable file.
    archive = tmp_path / "archive.dump"
    subprocess.run(["bash", "-c", f"umask 077; printf data > {archive}"], check=True)
    assert stat.S_IMODE(os.stat(archive).st_mode) == 0o600


def test_env_example_documents_compose_dollar_escaping() -> None:
    example = (ROOT / ".env.v2.example").read_text(encoding="utf-8")
    hash_section = example.split("NEMSEI_V2_ADMIN_PASSWORD_HASH=", 1)[0]
    assert "$$" in hash_section
    assert "interpolat" in hash_section.lower()


def test_truncated_admin_hash_reports_the_escaping_cause() -> None:
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://user:secret@localhost:5432/nemsei_v2_test",
        secret_key="test-secret",
        admin_username="admin",
        # What Compose delivers when `scrypt:32768:8:1$salt$hash` is unescaped.
        admin_password_hash="scrypt:32768:8:1",
        capabilities={"provider_reads": False, "provider_mutations": False, "notifications": False, "report_distribution": False},
        testing=True,
    )
    with pytest.raises(ConfigurationError, match=r"\$\$"):
        settings.validate(require_auth=True)
