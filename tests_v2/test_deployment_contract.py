from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


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
