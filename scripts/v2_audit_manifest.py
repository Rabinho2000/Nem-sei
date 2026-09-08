#!/usr/bin/env python3
"""Emit a baseline manifest of what this checkout, this database and this host
actually are, so an audit stops having to trust prose.

Three rules decide everything in here.

**Nothing is inferred.** Every field is either read from a live source or is the
string `"unknown"`. A number quoted from documentation, a count remembered from
a previous run, a service assumed to be up because a unit file exists on disk --
none of those are evidence, and writing them into a manifest is how a stale
comment becomes a fact nobody re-checks. `unknown` is a useful answer; a
plausible guess is not.

**Nothing is written.** Every database statement runs inside an explicitly
read-only transaction (`SET TRANSACTION READ ONLY`), so a mistake in a query
cannot become a mistake in the data. The manifest is a photograph.

**No secrets leave.** Configuration is reported as an allowlist of names that
are safe by construction, plus, for everything else, whether the variable is
set -- never its value. A database URL is rendered with its password hidden.
A manifest is a file people paste into tickets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNKNOWN = "unknown"

# Configuration whose value is safe to record: hostnames, switches, window
# sizes, identifiers. Anything not named here is reported as set/unset only.
SAFE_CONFIG_PREFIXES = (
    "NEMSEI_V2_ENV",
    "NEMSEI_V2_ROLE",
    "NEMSEI_V2_PROVIDER_READS",
    "NEMSEI_V2_PROVIDER_MUTATIONS",
    "NEMSEI_V2_NOTIFICATIONS",
    "NEMSEI_V2_REPORT_DISTRIBUTION",
    "NEMSEI_V2_TESTING",
    "NEMSEI_V2_HOST_DATA_ROOT",
    "NEMSEI_V2_PRODUCTION_",
    "NEMSEI_V2_AVAILABILITY_",
    "NEMSEI_V2_SCHEDULER_",
    "NEMSEI_V2_WORKER_",
    "NEMSEI_V2_JOB_",
    "NEMSEI_V2_HUAWEI_SCADA_",
    "NEMSEI_V2_SCADA_",
    "NEMSEI_V2_NOTIFICATION_",
    "NEMSEI_V2_DIGEST_",
)
# ...unless the name itself says otherwise. A prefix match is a blunt rule and
# this is the guard that keeps it from ever being the wrong one.
SECRET_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "DATABASE_URL", "HASH")


def _is_safe_config(name: str) -> bool:
    if any(marker in name for marker in SECRET_MARKERS):
        return False
    return any(name.startswith(prefix) for prefix in SAFE_CONFIG_PREFIXES)


def _run(
    command: list[str], *, cwd: Path | None = None, timeout: int = 30, env: dict[str, str] | None = None
) -> str | None:
    """The command's stdout, or None. A missing tool is an unknown, not a crash."""
    try:
        completed = subprocess.run(
            command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def code_section(root: Path) -> dict[str, Any]:
    sha = _run(["git", "rev-parse", "HEAD"], cwd=root) or UNKNOWN
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root) or UNKNOWN
    status = _run(["git", "status", "--porcelain"], cwd=root)
    versions = root / "migrations" / "versions"
    revisions = sorted(path.stem for path in versions.glob("*.py")) if versions.is_dir() else []
    return {
        "repository_root": str(root),
        "git_sha": sha,
        "git_branch": branch,
        # A dirty tree means the SHA does not describe what is deployed, which
        # is the single most misleading thing a baseline can get wrong.
        "working_tree_clean": (status == "") if status is not None else UNKNOWN,
        "uncommitted_paths": sorted(line[3:] for line in status.splitlines()) if status else [],
        "migration_revisions_on_disk": len(revisions),
        "newest_migration_file": revisions[-1] if revisions else UNKNOWN,
        "alembic_heads": _alembic_heads(root),
    }


def _alembic_heads(root: Path) -> Any:
    # `sys.executable`, never a bare "python": the interpreter that can import
    # alembic is the one running this script, and picking up whatever the PATH
    # happens to offer is how this field silently became "unknown".
    output = _run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    if output is None:
        return UNKNOWN
    return [line.split()[0] for line in output.splitlines() if line.strip()]


def configuration_section(env_file: Path | None) -> dict[str, Any]:
    """Effective V2 configuration, sanitised.

    Reads the process environment and, when given, the deployment's env file --
    the two disagree often enough (a variable exported in a shell, a file the
    containers actually mount) that recording only one of them is how a
    baseline ends up describing a deployment nobody is running.
    """
    process = {name: value for name, value in os.environ.items() if name.startswith("NEMSEI_V2_")}
    file_values: dict[str, str] = {}
    file_state: Any = UNKNOWN
    if env_file is not None:
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name, _, value = stripped.partition("=")
                file_values[name.strip()] = value.strip()
            file_state = "read"
        except OSError:
            file_state = "unreadable"
    return {
        "env_file": str(env_file) if env_file else UNKNOWN,
        "env_file_state": file_state,
        "process_environment": _sanitise(process),
        "env_file_values": _sanitise(file_values),
        # Consumed by `build_manifest` and removed before serialisation: the
        # capability view needs the raw switches, and the manifest must not
        # carry a second, unsanitised copy of the file.
        "_raw_env_file_values": file_values,
    }


def _sanitise(values: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in sorted(values):
        value = values[name]
        out[name] = value if _is_safe_config(name) else ("<set>" if value.strip() else "<empty>")
    return out


def capabilities_section(env_file_values: dict[str, str] | None = None) -> dict[str, Any]:
    """The four external switches, from this process and from the env file.

    They disagree in practice -- an auditor's shell has none of them set while
    the deployment's file has them on -- and collapsing the two into one
    "effective" answer would describe a process nobody is running as though it
    were the deployment.
    """
    return {
        "process_environment": _capability_view(os.environ),
        "env_file": _capability_view(env_file_values) if env_file_values is not None else UNKNOWN,
    }


def _capability_view(values) -> dict[str, Any]:
    from nemsei.config import CAPABILITIES, parse_bool

    result: dict[str, Any] = {}
    for capability in CAPABILITIES:
        raw = values.get(f"NEMSEI_V2_{capability.upper()}")
        if raw is None:
            # Default-deny is what the code does, but this manifest reports what
            # was *configured*; "not set" and "set to false" are different facts
            # about a deployment even when they behave the same.
            result[capability] = {"configured": False, "effective": False}
            continue
        try:
            result[capability] = {"configured": True, "effective": parse_bool(raw)}
        except ValueError:
            result[capability] = {"configured": True, "effective": UNKNOWN}
    return result


READ_ONLY_COUNTS: dict[str, str] = {
    "organizations": "SELECT count(*) FROM organizations",
    "installations": "SELECT count(*) FROM installations",
    "assets": "SELECT count(*) FROM assets",
    "devices": "SELECT count(*) FROM devices",
    "provider_connections": "SELECT count(*) FROM provider_connections",
    "provider_connections_enabled": "SELECT count(*) FROM provider_connections WHERE enabled",
    "asset_provider_mappings": "SELECT count(*) FROM asset_provider_mappings",
    "asset_provider_mappings_active": "SELECT count(*) FROM asset_provider_mappings WHERE mapping_status = 'active'",
    "asset_source_policies": "SELECT count(*) FROM asset_source_policies",
    "production_facts": "SELECT count(*) FROM production_facts",
    "monitoring_observations": "SELECT count(*) FROM monitoring_observations",
    "sync_runs": "SELECT count(*) FROM sync_runs",
    "sync_cursors": "SELECT count(*) FROM sync_cursors",
    "jobs": "SELECT count(*) FROM jobs",
    "jobs_active": "SELECT count(*) FROM jobs WHERE status IN ('queued','running','waiting')",
    "schedule_states": "SELECT count(*) FROM schedule_state",
    "asset_service_contracts": "SELECT count(*) FROM asset_service_contracts",
    "report_snapshots": "SELECT count(*) FROM report_snapshots",
}

READ_ONLY_BREAKDOWNS: dict[str, str] = {
    "connections_by_provider": (
        "SELECT provider_code, enabled, configuration_status, count(*) "
        "FROM provider_connections GROUP BY 1,2,3 ORDER BY 1,2,3"
    ),
    "active_mappings_by_provider": (
        "SELECT c.provider_code, m.resource_kind, count(*) "
        "FROM asset_provider_mappings m JOIN provider_connections c ON c.id = m.provider_connection_id "
        "WHERE m.mapping_status = 'active' GROUP BY 1,2 ORDER BY 1,2"
    ),
    "source_policies_by_use": (
        "SELECT source_use, is_fallback, count(*) FROM asset_source_policies GROUP BY 1,2 ORDER BY 1,2"
    ),
    "jobs_by_status": "SELECT status, count(*) FROM jobs GROUP BY 1 ORDER BY 1",
    "sync_runs_by_status": "SELECT status, completeness, count(*) FROM sync_runs GROUP BY 1,2 ORDER BY 1,2",
    "production_cursors": (
        "SELECT provider_connection_id, cursor_key, covered_through, checkpoint_json "
        "FROM sync_cursors ORDER BY provider_connection_id, cursor_key"
    ),
    "assets_with_multiple_active_mappings": (
        "SELECT m.asset_id, count(*) FROM asset_provider_mappings m "
        "WHERE m.mapping_status = 'active' AND m.resource_kind = 'plant' "
        "GROUP BY 1 HAVING count(*) > 1 ORDER BY 1"
    ),
    "integration_health": (
        "SELECT provider_connection_id, sync_state, partial, stale, last_success_at, "
        "last_successful_sync_at, last_failure_at, last_error_code FROM integration_health "
        "ORDER BY provider_connection_id"
    ),
}


def database_section(database_url: str | None) -> dict[str, Any]:
    if not database_url:
        return {"state": "no_database_url", "applied_revision": UNKNOWN, "counts": UNKNOWN, "breakdowns": UNKNOWN}
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    try:
        engine = create_engine(database_url, pool_pre_ping=True)
    except Exception as exc:  # noqa: BLE001 - a manifest reports, it does not fail
        return {"state": f"unusable_url: {type(exc).__name__}", "applied_revision": UNKNOWN}
    section: dict[str, Any] = {
        "url": make_url(database_url).render_as_string(hide_password=True),
        "state": UNKNOWN,
        "applied_revision": UNKNOWN,
        "counts": {},
        "breakdowns": {},
    }
    try:
        with engine.connect() as connection:
            # Read-only for the whole session: a manifest that could write is a
            # manifest nobody should run against production.
            connection.execute(text("SET TRANSACTION READ ONLY"))
            section["state"] = "connected"
            section["server_version"] = str(connection.execute(text("SHOW server_version")).scalar())
            section["applied_revision"] = str(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            )
            for name, statement in READ_ONLY_COUNTS.items():
                section["counts"][name] = _scalar(connection, statement)
            for name, statement in READ_ONLY_BREAKDOWNS.items():
                section["breakdowns"][name] = _rows(connection, statement)
    except Exception as exc:  # noqa: BLE001
        section["state"] = f"unreachable: {type(exc).__name__}"
    finally:
        engine.dispose()
    return section


def _scalar(connection, statement: str) -> Any:
    from sqlalchemy import text

    try:
        return int(connection.execute(text(statement)).scalar() or 0)
    except Exception:  # noqa: BLE001
        return UNKNOWN


def _rows(connection, statement: str) -> Any:
    from sqlalchemy import text

    try:
        return [[_jsonable(value) for value in row] for row in connection.execute(text(statement)).all()]
    except Exception:  # noqa: BLE001
        return UNKNOWN


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (dict, list)):
        return value
    return str(value)


def services_section(compose_project: str) -> dict[str, Any]:
    """What is actually running, when the host lets us look.

    Each container's own environment is read, not just the deployment's env
    file. Compose overrides win over the file, and on 2026-09-07 they did:
    `.env.v2` said `NEMSEI_V2_PROVIDER_READS=false` while every running
    container had it `true`. A baseline that had reported only the file would
    have described a deployment that makes no provider calls, next to a
    database full of provider calls.
    """
    containers = _run(
        [
            "docker", "ps", "--filter", f"label=com.docker.compose.project={compose_project}",
            "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}",
        ]
    )
    if containers is None:
        running: Any = UNKNOWN
    else:
        running = []
        for line in containers.splitlines():
            if not line.strip():
                continue
            entry = dict(zip(("name", "image", "status"), line.split("\t")))
            entry["environment"] = _container_environment(entry["name"])
            entry["capabilities"] = (
                _capability_view(entry["environment"]) if isinstance(entry["environment"], dict) else UNKNOWN
            )
            running.append(entry)
    timers = _run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
    if timers is None:
        backup_timer: Any = UNKNOWN
    else:
        matches = [line.strip() for line in timers.splitlines() if "nemsei" in line]
        backup_timer = matches or "no_nemsei_timer_listed"
    return {"compose_project": compose_project, "containers": running, "systemd_timers": backup_timer}


def _container_environment(name: str) -> Any:
    """One container's NEMSEI_V2_* environment, sanitised the same way."""
    raw = _run(["docker", "inspect", name, "--format", "{{range .Config.Env}}{{println .}}{{end}}"])
    if raw is None:
        return UNKNOWN
    values: dict[str, str] = {}
    for line in raw.splitlines():
        name_part, _, value = line.partition("=")
        if name_part.startswith("NEMSEI_V2_"):
            values[name_part] = value
    return _sanitise(values)


def backups_section(backup_dir: Path) -> dict[str, Any]:
    if not backup_dir.is_dir():
        return {"directory": str(backup_dir), "state": UNKNOWN, "archives": UNKNOWN}
    archives = []
    try:
        for entry in sorted(backup_dir.iterdir()):
            if not entry.is_file():
                continue
            stat = entry.stat()
            archives.append(
                {
                    "name": entry.name,
                    "bytes": stat.st_size,
                    "mode": oct(stat.st_mode & 0o777),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    # A dump that never got renamed is not a backup, and a
                    # manifest that lists it alongside the real ones invites
                    # exactly the mistake this distinction exists to prevent.
                    "partial": entry.name.endswith(".partial"),
                }
            )
    except OSError:
        return {"directory": str(backup_dir), "state": "unreadable", "archives": UNKNOWN}
    return {
        "directory": str(backup_dir),
        "state": "read",
        "archives": archives[-10:],
        "archive_count": len(archives),
        # Whether a restore was ever rehearsed is not knowable from a directory
        # listing, and guessing it would be the worst possible field to guess.
        "last_verified_restore": UNKNOWN,
    }


def build_manifest(
    *, root: Path, database_url: str | None, env_file: Path | None, compose_project: str, backup_dir: Path
) -> dict[str, Any]:
    configuration = configuration_section(env_file)
    # Popped, not copied: the capability view needs the raw switches, and the
    # manifest must never carry a second, unsanitised copy of the file.
    raw_env_file = configuration.pop("_raw_env_file_values")
    return {
        "manifest_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "host": os.uname().nodename,
        "code": code_section(root),
        "configuration": configuration,
        "capabilities": capabilities_section(raw_env_file),
        "database": database_section(database_url),
        "services": services_section(compose_project),
        "backups": backups_section(backup_dir),
    }


SECRET_SHAPES = (
    re.compile(r"(?i)\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    # Any URL still carrying a password. The lookahead spares the masked form
    # SQLAlchemy's own `hide_password=True` produces (`://user:***@host`),
    # which is the redaction working rather than a leak.
    re.compile(r"(?i)://[^:/@\s]+:(?!\*+@)[^@/\s]+@"),
    re.compile(r"(?i)\bscrypt:[0-9]+:[0-9]+:[0-9]+\$"),  # werkzeug password hash
)


def secret_findings(document: str) -> list[str]:
    """Shapes that must never appear in a manifest. The last line of defence."""
    return [pattern.pattern for pattern in SECRET_SHAPES if pattern.search(document)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Baseline manifest for a V2 checkout, database and host.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--database-url", default=os.environ.get("NEMSEI_V2_AUDIT_DATABASE_URL", ""))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--compose-project", default=os.environ.get("NEMSEI_V2_COMPOSE_PROJECT", "nemsei-v2"))
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(os.environ.get("NEMSEI_V2_HOST_DATA_ROOT", "/opt/server/apps/Nem-sei-v2-data")) / "backups",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    manifest = build_manifest(
        root=args.root,
        database_url=args.database_url or None,
        env_file=args.env_file,
        compose_project=args.compose_project,
        backup_dir=args.backup_dir,
    )
    document = json.dumps(manifest, indent=2, sort_keys=True)
    findings = secret_findings(document)
    if findings:
        # Refusing to emit is the only safe failure here: a manifest is written
        # to be shared, and a redaction bug caught after publication is not
        # caught at all.
        print(f"Manifest withheld: it matched {len(findings)} secret shape(s).", flush=True)
        return 2
    if args.output:
        args.output.write_text(document + "\n", encoding="utf-8")
        args.output.chmod(0o600)
        print(f"Wrote {args.output}")
    else:
        print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
