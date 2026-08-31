from __future__ import annotations

import importlib.util
import json
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


def scada_intent():
    """Load the SCADA deployment-intent helper, which also lives in scripts/."""
    path = ROOT / "scripts/v2_scada_deployment_intent.py"
    spec = importlib.util.spec_from_file_location("v2_scada_deployment_intent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rendered_config(connection_id="6", host_ip="192.168.1.237", profiles=("huawei-scada",),
                    replicas=None, second_publisher=False):
    """A rendered `docker compose config --format json`, trimmed to what is read."""
    listener = {
        "profiles": list(profiles),
        "environment": {"NEMSEI_V2_HUAWEI_SCADA_LISTENER_CONNECTION_ID": connection_id},
        "ports": [{"mode": "ingress", "host_ip": host_ip, "target": 1502,
                   "published": "1502", "protocol": "tcp"}],
    }
    if replicas is not None:
        listener["deploy"] = {"replicas": replicas}
    services = {"postgres": {}, "web": {}, "scada-listener": listener}
    if second_publisher:
        services["web"] = {"ports": [{"host_ip": "127.0.0.1", "target": 1502, "published": "1503"}]}
    return {"services": services}


def test_a_deployment_that_names_a_connection_declares_scada() -> None:
    assert scada_intent().declares_scada(rendered_config()) is True


@pytest.mark.parametrize("connection_id", ["", "   ", None])
def test_a_deployment_without_a_connection_id_does_not_declare_scada(connection_id) -> None:
    """The listener refuses to run without one, so this is the whole signal."""
    assert scada_intent().declares_scada(rendered_config(connection_id=connection_id)) is False


def test_a_configuration_without_the_listener_service_does_not_declare_scada() -> None:
    config = rendered_config()
    del config["services"]["scada-listener"]
    assert scada_intent().declares_scada(config) is False


@pytest.mark.parametrize("host_ip", ["0.0.0.0", "", "::", None])
def test_a_listener_bound_to_every_interface_is_refused(host_ip) -> None:
    """The pilot runs without TLS; the bind address is the only thing narrowing it."""
    module = scada_intent()
    with pytest.raises(module.ScadaDeploymentError, match="every interface"):
        module.declares_scada(rendered_config(host_ip=host_ip))


def test_a_listener_outside_its_profile_is_refused() -> None:
    module = scada_intent()
    with pytest.raises(module.ScadaDeploymentError, match="profile"):
        module.declares_scada(rendered_config(profiles=()))


def test_a_second_service_publishing_the_scada_port_is_refused() -> None:
    module = scada_intent()
    with pytest.raises(module.ScadaDeploymentError, match="Exactly one service"):
        module.declares_scada(rendered_config(second_publisher=True))


def test_more_than_one_listener_replica_is_refused() -> None:
    """One socket, and one holder of the advisory lock in listener.py."""
    module = scada_intent()
    with pytest.raises(module.ScadaDeploymentError, match="exactly once"):
        module.declares_scada(rendered_config(replicas=2))


def test_the_intent_helper_never_echoes_the_configuration_it_was_given() -> None:
    """The rendered config carries the secret key and the admin hash."""
    source = (ROOT / "scripts/v2_scada_deployment_intent.py").read_text(encoding="utf-8")
    printed = re.findall(r"^\s*print\((.*)\)\s*$", source, re.MULTILINE)
    assert printed == ['f"__NEMSEI_SCADA_DECLARED__={\'true\' if declared else \'false\'}"']


# --- the wrapper's two branches, driven end to end ----------------------------
#
# `v2_compose_up.sh` hardcodes `--project-name nemsei-v2`, so there is no way to
# run it for real without touching the live deployment. Standing a fake `docker`
# in front of it records the exact command sequence instead, which is the part
# that regressed: the listener was simply never named.

DOCKER_SHIM = """#!/usr/bin/env bash
args="$*"
printf '%s\n' "$args" >> "$DOCKER_CALLS"
case "$args" in
  *"config --format json"*) cat "$RENDERED_JSON" ;;
  *"ps --format"*)          printf '%s\n' "$SCADA_STATE" ;;
  *"psql"*)                 printf '%s\n' "$LIVE_REVISION" ;;
esac
exit 0
"""

# `.git` in this worktree points outside the test container, and the wrapper
# only wants the repository root.
GIT_SHIM = """#!/usr/bin/env bash
[[ $* == "rev-parse --show-toplevel" ]] && printf '%s\n' "$REPO_ROOT"
exit 0
"""

SLEEP_SHIM = "#!/usr/bin/env bash\nexit 0\n"


def run_wrapper(tmp_path, connection_id, scada_state="running"):
    """Run the canonical deploy against a recording `docker`.

    Returns the recorded command lines and the completed process, so a test can
    assert on either the sequence or the refusal.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", DOCKER_SHIM), ("git", GIT_SHIM), ("sleep", SLEEP_SHIM)):
        shim = bin_dir / name
        shim.write_text(body, encoding="utf-8")
        shim.chmod(0o755)

    listener = {
        "profiles": ["huawei-scada"],
        "environment": {"NEMSEI_V2_HUAWEI_SCADA_LISTENER_CONNECTION_ID": connection_id},
        "ports": [{"host_ip": "10.0.0.4", "target": 1502, "published": "1502"}],
    }
    rendered = tmp_path / "rendered.json"
    rendered.write_text(
        json.dumps({"services": {"postgres": {}, "web": {}, "scada-listener": listener}}),
        encoding="utf-8",
    )
    calls = tmp_path / "calls.txt"
    calls.touch()
    env_file = tmp_path / "env"
    env_file.write_text("NEMSEI_V2_ENV=production\n", encoding="utf-8")
    v1_root = tmp_path / "v1"
    v1_root.mkdir()

    completed = subprocess.run(
        ["bash", str(COMPOSE_UP)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOCKER_CALLS": str(calls),
            "RENDERED_JSON": str(rendered),
            "SCADA_STATE": scada_state,
            "LIVE_REVISION": "irrelevant-to-the-shim",
            "REPO_ROOT": str(ROOT),
            "NEMSEI_V2_DEPLOYMENT_MODE": "development",
            "NEMSEI_V1_DATA_ROOT": str(v1_root),
            "NEMSEI_V2_HOST_DATA_ROOT": str(tmp_path / "v2"),
            "NEMSEI_V2_ENV_FILE": str(env_file),
            "NEMSEI_V2_SCADA_READY_TIMEOUT_SECONDS": "0",
        },
        capture_output=True,
        text=True,
    )
    return [line for line in calls.read_text(encoding="utf-8").splitlines() if line], completed


def test_a_deployment_without_scada_never_touches_the_listener(tmp_path) -> None:
    calls, completed = run_wrapper(tmp_path, connection_id="")
    assert completed.returncode == 0, completed.stderr
    assert any("up -d web scheduler worker" in call for call in calls)
    assert any("run --rm migrate" in call for call in calls)
    assert not any("scada-listener" in call for call in calls)
    assert not any("--profile huawei-scada build" in call for call in calls)


def test_a_deployment_with_scada_builds_starts_and_checks_the_listener(tmp_path) -> None:
    calls, completed = run_wrapper(tmp_path, connection_id="6")
    assert completed.returncode == 0, completed.stderr
    assert any("up -d web scheduler worker" in call for call in calls)
    built = next(i for i, call in enumerate(calls) if "build scada-listener" in call)
    started = next(i for i, call in enumerate(calls) if "up -d scada-listener" in call)
    checked = next(i for i, call in enumerate(calls) if "ps --format" in call and "scada-listener" in call)
    assert built < started < checked
    # Every listener command carries the profile that keeps it out of ordinary runs.
    for call in calls:
        if "scada-listener" in call:
            assert "--profile huawei-scada" in call


def test_a_declared_listener_that_never_comes_up_fails_the_deploy(tmp_path) -> None:
    """The gate: losing the listener must not be reported as a successful deploy."""
    _calls, completed = run_wrapper(tmp_path, connection_id="6", scada_state="exited")
    assert completed.returncode == 1
    assert "declares Huawei SCADA but scada-listener is 'exited'" in completed.stderr


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


def test_every_compose_secret_path_can_be_redirected() -> None:
    """A clean checkout has none of these files, and CI must not need them.

    Compose bind-mounts every secret a service declares, whether or not the run
    reads one, so a hardcoded `./secrets/...` path stops a CI runner from
    starting any service at all -- which is exactly how the Docker recovery
    acceptance failed the first time this workflow ran on a real runner.
    """
    compose = (ROOT / "docker-compose.v2.yml").read_text(encoding="utf-8")
    block = compose.split("\nsecrets:\n", 1)[1]
    # Not `\S+`: the two required ones carry a `:?message` with a space in it.
    paths = re.findall(r"^\s*file:\s*(.+?)\s*$", block, re.MULTILINE)
    assert len(paths) == 8, paths
    for path in paths:
        assert path.startswith("${"), f"{path} cannot be redirected away from the repository"


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


def test_deployment_rebuilds_every_image_it_is_about_to_run() -> None:
    # `migrate` sits behind the `manual` profile, so a plain `compose build`
    # skips it and a stale image migrates to its own older head. A stale web
    # image is the mirror image of the same fault: it carries an older graph
    # than the migrated database and fails readiness.
    script = COMPOSE_UP.read_text(encoding="utf-8")
    build = script.index("--profile manual build")
    migrate = script.index("run --rm migrate")
    startup = script.index("up -d web scheduler worker")
    assert build < migrate < startup
    # The build must not be narrowed to a single service again.
    assert "--profile manual build\n" in script


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
