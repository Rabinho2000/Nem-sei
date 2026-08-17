from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "v2_resolve_alembic_head.py"
SPEC = importlib.util.spec_from_file_location("v2_resolve_alembic_head", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_revision(path: Path, revision: str, down_revision: str | None) -> None:
    path.write_text(
        f"revision = {revision!r}\ndown_revision = {down_revision!r}\n",
        encoding="utf-8",
    )


def write_config(path: Path) -> Path:
    config = path / "alembic.ini"
    migrations = path / "migrations"
    config.write_text(f"[alembic]\nscript_location = {migrations}\n", encoding="utf-8")
    (migrations / "versions").mkdir(parents=True)
    return config


def test_resolves_current_repository_head_without_stale_literal(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    write_revision(tmp_path / "migrations" / "versions" / "001_base.py", "base", None)
    write_revision(tmp_path / "migrations" / "versions" / "002_current.py", "current", "base")
    assert MODULE.resolve_single_head(config) == "current"

    script = (ROOT / "scripts" / "v2_postgres_restore_smoke.sh").read_text(encoding="utf-8")
    assert "0004_asset_import_hardening" not in script
    assert "v2_resolve_alembic_head.py" in script
    assert "trap cleanup EXIT" in script
    assert "DROP DATABASE IF EXISTS" in script


def test_multiple_heads_fail_safely(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    write_revision(tmp_path / "migrations" / "versions" / "001_base.py", "base", None)
    write_revision(tmp_path / "migrations" / "versions" / "002_left.py", "left", "base")
    write_revision(tmp_path / "migrations" / "versions" / "003_right.py", "right", "base")
    with pytest.raises(MODULE.AlembicHeadError, match="multiple heads"):
        MODULE.resolve_single_head(config)


def test_empty_head_fails_safely(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    with pytest.raises(MODULE.AlembicHeadError, match="no head"):
        MODULE.resolve_single_head(config)


def test_restored_revision_match_and_mismatch() -> None:
    MODULE.validate_restored_revision("current", "current")
    with pytest.raises(MODULE.AlembicHeadError, match="does not match"):
        MODULE.validate_restored_revision("stale", "current")
    with pytest.raises(MODULE.AlembicHeadError, match="no Alembic revision"):
        MODULE.validate_restored_revision("", "current")
