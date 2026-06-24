from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from monitoring_board.runtime import UPLOAD_DIR, path_is_within, resolve_runtime_file_path_within


@dataclass(frozen=True)
class StorageFinding:
    status: str
    path: str
    file_id: int | None = None
    run_id: int | None = None
    detail: str = ""


def reconcile_generated_reports(conn: sqlite3.Connection, *, root: Path | None = None, cleanup: bool = False, stale_hours: int = 24) -> list[StorageFinding]:
    root = (root or (UPLOAD_DIR / "generated_reports")).resolve()
    findings: list[StorageFinding] = []
    registered: set[Path] = set()
    rows = conn.execute("SELECT * FROM report_generated_files").fetchall()
    for row in rows:
        stored = str(row["relative_path"] or "")
        path = resolve_runtime_file_path_within(stored, root) if stored else None
        if path is None:
            findings.append(StorageFinding("invalid_path", stored, row["id"], row["run_id"]))
            continue
        if path.is_symlink():
            findings.append(StorageFinding("unexpected_symlink", str(path), row["id"], row["run_id"]))
            continue
        registered.add(path.resolve())
        if row["status"] != "completed":
            continue
        if not path.exists():
            findings.append(StorageFinding("missing_file", str(path), row["id"], row["run_id"]))
            continue
        if path.stat().st_size != int(row["size_bytes"] or 0):
            findings.append(StorageFinding("size_mismatch", str(path), row["id"], row["run_id"]))
        if row["sha256"] and hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            findings.append(StorageFinding("hash_mismatch", str(path), row["id"], row["run_id"]))
        if not any(item.path == str(path) and item.file_id == row["id"] for item in findings):
            findings.append(StorageFinding("ok", str(path), row["id"], row["run_id"]))

    if root.exists():
        cutoff = datetime.now() - timedelta(hours=stale_hours)
        for path in iter_storage_files(root):
            resolved = path.resolve()
            if not path_is_within(resolved, root):
                findings.append(StorageFinding("invalid_path", str(path)))
                continue
            if ".staging" in path.parts:
                status = "stale_staging" if datetime.fromtimestamp(path.stat().st_mtime) < cutoff else "ok"
                findings.append(StorageFinding(status, str(path)))
                if cleanup and status == "stale_staging":
                    path.unlink(missing_ok=True)
                continue
            if resolved not in registered:
                findings.append(StorageFinding("orphan_file", str(path)))
                if cleanup:
                    path.unlink(missing_ok=True)
    return findings


def iter_storage_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path
