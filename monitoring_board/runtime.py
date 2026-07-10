from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path
    database: Path
    backups: Path
    uploads: Path
    contracts: Path
    logs: Path


def resolve_data_dir(data_dir_value: str | None = None) -> Path:
    raw_value = os.environ.get("DATA_DIR", "") if data_dir_value is None else data_dir_value
    if not raw_value or not raw_value.strip():
        return BASE_DIR

    configured_path = Path(raw_value.strip()).expanduser()
    if not configured_path.is_absolute():
        configured_path = BASE_DIR / configured_path
    return configured_path.resolve()


def build_runtime_paths(data_dir_value: str | None = None) -> RuntimePaths:
    data_dir = resolve_data_dir(data_dir_value)
    uploads_dir = data_dir / "uploads"
    return RuntimePaths(
        data_dir=data_dir,
        database=data_dir / "monitoring_board.db",
        backups=data_dir / "backups",
        uploads=uploads_dir,
        contracts=uploads_dir / "contracts",
        logs=data_dir / "logs",
    )


def ensure_runtime_directories(runtime_paths: RuntimePaths) -> None:
    for directory in (
        runtime_paths.data_dir,
        runtime_paths.backups,
        runtime_paths.uploads,
        runtime_paths.contracts,
        runtime_paths.logs,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def store_runtime_relative_path(path: Path) -> str:
    return str(path.relative_to(RUNTIME_PATHS.data_dir))


def resolve_runtime_file_path(stored_path: str) -> Path:
    path = Path(stored_path)
    if path.is_absolute():
        return path

    data_path = RUNTIME_PATHS.data_dir / path
    if data_path.exists():
        return data_path
    return BASE_DIR / path


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def max_upload_bytes() -> int:
    raw_value = os.environ.get("MAX_UPLOAD_MB", "").strip()
    if raw_value.isdigit() and int(raw_value) > 0:
        return int(raw_value) * 1024 * 1024
    return DEFAULT_MAX_UPLOAD_BYTES


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def resolve_runtime_file_path_within(stored_path: str, allowed_dir: Path) -> Path | None:
    path = Path(stored_path)
    candidates = [path] if path.is_absolute() else [RUNTIME_PATHS.data_dir / path, BASE_DIR / path]
    for candidate in candidates:
        if path_is_within(candidate, allowed_dir):
            return candidate.resolve()
    return None


load_local_env()

RUNTIME_PATHS = build_runtime_paths()
DB_PATH = RUNTIME_PATHS.database
DEFAULT_EXCEL_PATH = next(BASE_DIR.glob("*.xlsx"), None)
BACKUP_DIR = RUNTIME_PATHS.backups
UPLOAD_DIR = RUNTIME_PATHS.uploads
CONTRACTS_DIR = RUNTIME_PATHS.contracts
LOG_DIR = RUNTIME_PATHS.logs
