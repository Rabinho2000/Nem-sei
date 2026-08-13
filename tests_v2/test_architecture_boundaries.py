from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "nemsei"


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
    return found


def test_v2_never_imports_v1() -> None:
    violations = [str(path.relative_to(ROOT)) for path in [*SOURCE.rglob("*.py"), *(ROOT / "tests_v2").rglob("*.py")] if any(name == "monitoring_board" or name.startswith("monitoring_board.") for name in imports(path))]
    assert not violations


def test_non_web_modules_do_not_import_flask() -> None:
    violations = [str(path.relative_to(ROOT)) for path in SOURCE.rglob("*.py") if "web" not in path.parts and path.name not in {"app.py", "wsgi.py"} and any(name == "flask" or name.startswith("flask.") for name in imports(path))]
    assert not violations


def test_scheduler_cannot_import_handlers_or_execute_business_services() -> None:
    scheduler_imports = imports(SOURCE / "jobs" / "scheduler.py")
    assert "nemsei.jobs.handlers" not in scheduler_imports
    assert "nemsei.system.noop_service" not in scheduler_imports


def test_app_composition_root_has_no_direct_sql_execution() -> None:
    source = (SOURCE / "app.py").read_text(encoding="utf-8")
    assert "exec_driver_sql" not in source
    assert ".execute(" not in source
