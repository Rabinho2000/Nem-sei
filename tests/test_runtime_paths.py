from __future__ import annotations

from app import BASE_DIR, build_runtime_paths, ensure_runtime_directories


def test_runtime_paths_default_to_project_directory() -> None:
    paths = build_runtime_paths("")

    assert paths.data_dir == BASE_DIR
    assert paths.database == BASE_DIR / "monitoring_board.db"
    assert paths.backups == BASE_DIR / "backups"
    assert paths.uploads == BASE_DIR / "uploads"
    assert paths.contracts == BASE_DIR / "uploads" / "contracts"
    assert paths.logs == BASE_DIR / "logs"


def test_runtime_paths_use_configured_absolute_data_dir(tmp_path) -> None:
    data_dir = tmp_path / "data"

    paths = build_runtime_paths(str(data_dir))

    assert paths.data_dir == data_dir.resolve()
    assert paths.database == data_dir.resolve() / "monitoring_board.db"
    assert paths.backups == data_dir.resolve() / "backups"
    assert paths.uploads == data_dir.resolve() / "uploads"
    assert paths.contracts == data_dir.resolve() / "uploads" / "contracts"
    assert paths.logs == data_dir.resolve() / "logs"


def test_runtime_paths_resolve_relative_data_dir_from_project_directory() -> None:
    paths = build_runtime_paths("runtime-data")

    assert paths.data_dir == (BASE_DIR / "runtime-data").resolve()
    assert paths.database == (BASE_DIR / "runtime-data" / "monitoring_board.db").resolve()


def test_ensure_runtime_directories_creates_required_directories(tmp_path) -> None:
    paths = build_runtime_paths(str(tmp_path / "data"))

    ensure_runtime_directories(paths)

    assert paths.data_dir.is_dir()
    assert paths.backups.is_dir()
    assert paths.uploads.is_dir()
    assert paths.contracts.is_dir()
    assert paths.logs.is_dir()
