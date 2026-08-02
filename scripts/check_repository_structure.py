"""Fail fast on stale runtime artefacts and broken template route references."""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
URL_FOR_PATTERN = re.compile(r"url_for\(\s*['\"]([^'\"]+)['\"]")
FORBIDDEN_RUNTIME_PATHS = {
    "fusionsolar_raw_export.py",
    "resultado_onboard.json",
}
FORBIDDEN_RUNTIME_PREFIXES = ("diagnostics/",)


def tracked_paths() -> set[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return {
        decoded
        for path in output.split(b"\0")
        if path
        for decoded in (path.decode("utf-8"),)
        if (ROOT / decoded).is_file()
    }


def route_endpoints() -> set[str]:
    endpoints = {"static"}
    for path in (ROOT / "monitoring_board").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        blueprints: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            if not isinstance(node.value.func, ast.Name) or node.value.func.id != "Blueprint":
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            if node.value.args and isinstance(node.value.args[0], ast.Constant):
                blueprints[node.targets[0].id] = str(node.value.args[0].value)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr not in {"route", "get", "post", "put", "delete", "patch"}:
                    continue
                owner = decorator.func.value
                if isinstance(owner, ast.Name) and owner.id in blueprints:
                    endpoints.add(f"{blueprints[owner.id]}.{node.name}")
                elif isinstance(owner, ast.Name) and owner.id == "app":
                    endpoints.add(node.name)
    return endpoints


def referenced_template_endpoints() -> set[str]:
    endpoints: set[str] = set()
    for path in (ROOT / "templates").rglob("*.html"):
        endpoints.update(URL_FOR_PATTERN.findall(path.read_text(encoding="utf-8")))
    return endpoints


def main() -> int:
    tracked = tracked_paths()
    stale_artifacts = sorted(
        path
        for path in tracked
        if path in FORBIDDEN_RUNTIME_PATHS or path.startswith(FORBIDDEN_RUNTIME_PREFIXES)
    )
    missing_endpoints = sorted(referenced_template_endpoints() - route_endpoints())
    if stale_artifacts:
        print("Artefactos operacionais não podem ser versionados:")
        for path in stale_artifacts:
            print(f"- {path}")
    if missing_endpoints:
        print("Templates referem endpoints sem rota:")
        for endpoint in missing_endpoints:
            print(f"- {endpoint}")
    if stale_artifacts or missing_endpoints:
        return 1
    print("Estrutura: rotas de templates e artefactos operacionais verificados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
