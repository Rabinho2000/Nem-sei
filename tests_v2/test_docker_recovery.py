from __future__ import annotations

from pathlib import Path


def test_docker_recovery_acceptance_uses_actual_worker_stop_restart() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "run_docker_recovery_acceptance.sh").read_text(encoding="utf-8")
    assert "kill -s SIGKILL worker" in script
    assert "lease_recovered" not in script
    assert "('recovery', 'running', 'waiting')" in script
    assert "up -d worker" in script
