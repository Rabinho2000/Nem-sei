from __future__ import annotations

from pathlib import Path


def test_docker_recovery_acceptance_uses_actual_worker_stop_restart() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (Path(__file__).resolve().parents[1] / "scripts" / "run_docker_recovery_acceptance.sh").read_text(encoding="utf-8")
    assert "kill -s SIGKILL worker" in script
    assert "lease_recovered" not in script
    assert "('recovery', 'running', 'waiting')" in script
    assert "up -d worker" in script
    assert "--profile acceptance" in script
    dockerfile = (root / "Dockerfile.v2").read_text(encoding="utf-8")
    assert dockerfile.rfind("FROM runtime AS production") > dockerfile.index("FROM runtime AS acceptance")
    acceptance = (root / "docker-compose.v2.acceptance.yml").read_text(encoding="utf-8")
    assert "target: acceptance" in acceptance
    assert 'profiles: ["acceptance"]' in acceptance
