from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_production_compose_defaults_to_the_dedicated_v2_host_root() -> None:
    compose = (ROOT / "docker-compose.v2.yml").read_text(encoding="utf-8")
    assert "/opt/server/apps/Nem-sei-v2-data" in compose
    assert ":/data" in compose


def test_canonical_deployment_validates_before_starting_roles() -> None:
    script = (ROOT / "scripts/v2_compose_up.sh").read_text(encoding="utf-8")
    preflight = script.index("verify_v2_runtime_isolation.py")
    migrate = script.index('run --rm migrate')
    startup = script.index("up -d web scheduler worker")
    assert preflight < migrate < startup
    assert "config --format json" in script
    assert "NEMSEI_V2_WORKER_SCALE" in script
    assert "accepts no Compose scale arguments" in script
