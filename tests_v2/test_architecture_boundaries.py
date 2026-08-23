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
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
    return found


def test_v2_never_imports_v1() -> None:
    # Golden parity tests are the one declared exception: they load the frozen V1
    # only to compare against it, and they load it dynamically, so no V2 source
    # or ordinary test carries a V1 import.
    candidates = [*SOURCE.rglob("*.py"), *(path for path in (ROOT / "tests_v2").rglob("*.py") if not path.name.endswith("_golden.py"))]
    violations = [str(path.relative_to(ROOT)) for path in candidates if any(name == "monitoring_board" or name.startswith("monitoring_board.") for name in imports(path))]
    assert not violations


def test_no_v2_source_file_reaches_v1_dynamically() -> None:
    """A dynamic import would slip past the static check above.

    Naming V1 in a docstring is fine and often useful, because several V2
    modules are ports and should say so. Naming it in a string the code can act
    on is not.
    """
    offenders = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                if "monitoring_board" in node.value:
                    offenders.append(f"{path.relative_to(ROOT)}: {node.value[:60]!r}")
    assert offenders == []


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


def test_network_dependency_is_confined_to_provider_http_clients() -> None:
    prohibited = {"requests", "httpx", "aiohttp", "urllib", "urllib3", "socket"}
    violations = []
    packages = (SOURCE / "assets", SOURCE / "providers", SOURCE / "sync", SOURCE / "monitoring", SOURCE / "sources", SOURCE / "integrations")
    allowed = {
        SOURCE / "integrations" / "fusionsolar" / "client.py",
        SOURCE / "integrations" / "sigenergy" / "client.py",
        # A second, deliberately narrow network client: the V1 ownership
        # broker's lease API (docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md). Not
        # a provider client, but the same shape of concern this test
        # guards -- one small, single-purpose file making the only calls of
        # its kind, not network code scattered through the domain packages.
        SOURCE / "integrations" / "fusionsolar" / "v1_ownership.py",
    }
    for package in packages:
        for path in package.rglob("*.py"):
            names = imports(path)
            if path not in allowed and any(name.split(".", 1)[0] in prohibited for name in names):
                violations.append(str(path.relative_to(ROOT)))
    assert not violations


def test_fusionsolar_adapter_has_no_web_or_business_domain_dependencies() -> None:
    prohibited = ("flask", "nemsei.web", "nemsei.assets", "monitoring_board")
    violations = []
    for path in (SOURCE / "integrations" / "fusionsolar").rglob("*.py"):
        if any(name == item or name.startswith(f"{item}.") for item in prohibited for name in imports(path)):
            violations.append(str(path.relative_to(ROOT)))
    assert not violations


def test_sigenergy_adapter_has_no_web_or_business_domain_dependencies() -> None:
    prohibited = ("flask", "nemsei.web", "nemsei.assets", "monitoring_board")
    violations = []
    for path in (SOURCE / "integrations" / "sigenergy").rglob("*.py"):
        if any(name == item or name.startswith(f"{item}.") for item in prohibited for name in imports(path)):
            violations.append(str(path.relative_to(ROOT)))
    assert not violations


def test_monitoring_domain_never_imports_provider_specific_adapter_code() -> None:
    violations = [
        str(path.relative_to(ROOT))
        for path in (SOURCE / "monitoring").rglob("*.py")
        if any(name == "nemsei.integrations" or name.startswith("nemsei.integrations.") for name in imports(path))
    ]
    assert not violations


def test_sigenergy_is_not_leaked_into_canonical_domains() -> None:
    violations = []
    for package in (SOURCE / "assets", SOURCE / "providers", SOURCE / "sync", SOURCE / "monitoring", SOURCE / "sources"):
        for path in package.rglob("*.py"):
            text = path.read_text(encoding="utf-8").casefold()
            if any(field in text for field in ("sigencloud", "energyflow", "systemstatus", "pvpower", "battery soc")):
                violations.append(str(path.relative_to(ROOT)))
    assert not violations
