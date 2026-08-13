from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_DIRECTORIES = (
    PROJECT_ROOT / "monitoring_board" / "domains",
    PROJECT_ROOT / "monitoring_board" / "services",
    PROJECT_ROOT / "monitoring_board" / "repositories",
    PROJECT_ROOT / "monitoring_board" / "jobs",
)
FORBIDDEN_MODULES = {"app", "monitoring_board.app_factory"}
APP_FACTORY_MAX_LINES = 22_000
APP_FACTORY_MAX_DIRECT_ROUTES = 57


def _imports(path: Path) -> set[str]:
    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_domain_service_and_repository_modules_do_not_depend_on_composition_root() -> None:
    violations: list[str] = []
    for directory in PROTECTED_DIRECTORIES:
        if not directory.exists():
            continue
        for path in directory.rglob("*.py"):
            forbidden = _imports(path) & FORBIDDEN_MODULES
            if forbidden:
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {', '.join(sorted(forbidden))}")
    assert not violations, "\n".join(violations)


def test_app_factory_cannot_grow_or_gain_more_legacy_routes() -> None:
    """Keep migration pressure on the composition root without freezing refactors."""
    factory_path = PROJECT_ROOT / "monitoring_board" / "app_factory.py"
    source = factory_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(factory_path))
    direct_routes = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"route", "get", "post", "put", "delete", "patch"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "app"
    )
    assert len(source.splitlines()) <= APP_FACTORY_MAX_LINES
    assert direct_routes <= APP_FACTORY_MAX_DIRECT_ROUTES
