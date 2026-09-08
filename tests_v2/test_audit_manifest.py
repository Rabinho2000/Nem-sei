"""The baseline manifest: read-only, secret-free, and honest about gaps.

The manifest exists because an audit had to take the deployment's description
on trust. It is only worth having if three properties hold, and all three are
the kind that fail quietly: it must not write, it must not carry a secret, and
it must say `unknown` rather than something plausible.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def manifest_module():
    path = ROOT / "scripts/v2_audit_manifest.py"
    spec = importlib.util.spec_from_file_location("v2_audit_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- it must not carry a secret ----------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        '{"url": "postgresql+psycopg://nemsei:s3cr3t@db:5432/nemsei_v2"}',
        '{"token": "123456789:AAHfgHkLmNoPqRsTuVwXyZ1234567890abc"}',
        '{"hash": "scrypt:32768:8:1$abc$def"}',
    ],
)
def test_a_document_carrying_a_secret_shape_is_caught(document) -> None:
    assert manifest_module().secret_findings(document)


def test_the_redacted_form_of_a_url_is_not_mistaken_for_a_leak() -> None:
    """SQLAlchemy's own `hide_password=True` produces `://user:***@host`. The
    check firing on that would be the redaction working and the guard saying
    otherwise -- which is how a useful guard gets switched off."""
    assert not manifest_module().secret_findings('{"url": "postgresql+psycopg://nemsei:***@db:5432/nemsei_v2"}')


def test_a_configuration_value_that_is_not_on_the_allowlist_is_reduced_to_set() -> None:
    module = manifest_module()
    sanitised = module._sanitise(
        {
            "NEMSEI_V2_ENV": "preview",
            "NEMSEI_V2_SECRET_KEY": "a-real-secret",
            "NEMSEI_V2_ADMIN_PASSWORD_HASH": "scrypt:32768:8:1$x$y",
            "NEMSEI_V2_DATABASE_URL": "postgresql://u:p@h/d",
        }
    )

    assert sanitised["NEMSEI_V2_ENV"] == "preview"
    assert sanitised["NEMSEI_V2_SECRET_KEY"] == "<set>"
    assert sanitised["NEMSEI_V2_ADMIN_PASSWORD_HASH"] == "<set>"
    assert sanitised["NEMSEI_V2_DATABASE_URL"] == "<set>"


def test_a_name_matching_a_safe_prefix_is_still_refused_if_it_names_a_secret() -> None:
    """The prefix match is blunt; a name saying TOKEN or PASSWORD wins over it."""
    module = manifest_module()
    sanitised = module._sanitise(
        {
            "NEMSEI_V2_HUAWEI_SCADA_PRIMARY_POWER_UNIT": "kW",
            "NEMSEI_V2_HUAWEI_SCADA_PRIMARY_TOKEN": "abc123",
        }
    )

    assert sanitised["NEMSEI_V2_HUAWEI_SCADA_PRIMARY_POWER_UNIT"] == "kW"
    assert sanitised["NEMSEI_V2_HUAWEI_SCADA_PRIMARY_TOKEN"] == "<set>"


# --- it must say unknown rather than something plausible ----------------------


def test_an_unreachable_database_leaves_every_fact_unknown() -> None:
    module = manifest_module()

    section = module.database_section(None)

    assert section["applied_revision"] == module.UNKNOWN
    assert section["counts"] == module.UNKNOWN


def test_a_backup_directory_that_cannot_be_read_is_unknown_not_empty(tmp_path) -> None:
    """"No backups" and "we could not look" are different answers, and the
    second one must never be reported as the first."""
    module = manifest_module()

    section = module.backups_section(tmp_path / "does-not-exist")

    assert section["state"] == module.UNKNOWN
    assert section["archives"] == module.UNKNOWN


def test_whether_a_restore_was_ever_rehearsed_is_never_guessed(tmp_path) -> None:
    module = manifest_module()
    (tmp_path / "nemsei-v2-20260901T030000Z.dump").write_bytes(b"x")

    section = module.backups_section(tmp_path)

    assert section["last_verified_restore"] == module.UNKNOWN


def test_a_partial_dump_is_listed_as_partial(tmp_path) -> None:
    module = manifest_module()
    (tmp_path / "nemsei-v2-20260901T030000Z.dump.partial").write_bytes(b"x")

    archives = module.backups_section(tmp_path)["archives"]

    assert [entry["partial"] for entry in archives] == [True]


# --- it must not write --------------------------------------------------------


def test_every_database_statement_is_read_only() -> None:
    """A manifest that could write is a manifest nobody should run against
    production, so the queries are checked for anything that is not a SELECT."""
    module = manifest_module()
    statements = [*module.READ_ONLY_COUNTS.values(), *module.READ_ONLY_BREAKDOWNS.values()]

    assert statements
    for statement in statements:
        normalised = " ".join(statement.split()).upper()
        assert normalised.startswith(("SELECT", "WITH"))
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE "):
            assert forbidden not in normalised, statement


def test_the_session_is_put_into_a_read_only_transaction() -> None:
    source = (ROOT / "scripts/v2_audit_manifest.py").read_text(encoding="utf-8")
    assert "SET TRANSACTION READ ONLY" in source


# --- it must report one migration head ----------------------------------------


def test_the_manifest_reports_the_repository_migration_heads() -> None:
    module = manifest_module()

    code = module.code_section(ROOT)

    assert code["git_sha"] != module.UNKNOWN
    assert code["newest_migration_file"] != module.UNKNOWN
    # A second head means two migration lineages, which is a merge nobody did.
    assert isinstance(code["alembic_heads"], list) and len(code["alembic_heads"]) == 1


def test_the_manifest_runs_end_to_end_without_a_database(tmp_path) -> None:
    output = tmp_path / "manifest.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/v2_audit_manifest.py"),
         "--root", str(ROOT), "--backup-dir", str(tmp_path), "--output", str(output)],
        capture_output=True, text=True, check=False,
        # The script reads the capability names from `nemsei.config`, so it is
        # run the way the runbook runs it.
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["database"]["state"] == "no_database_url"
    assert "_raw_env_file_values" not in manifest["configuration"]
    # Written for one reader, not for a shared directory.
    assert oct(output.stat().st_mode & 0o777) == "0o600"
