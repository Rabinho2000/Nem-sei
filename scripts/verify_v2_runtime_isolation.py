#!/usr/bin/env python3
"""Validate that deployment paths physically isolate V2 runtime data."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


V2_DATABASE_FILENAME = "nemsei_v2.db"


def contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v1-data-root", default=os.environ.get("NEMSEI_V1_DATA_ROOT", ""))
    parser.add_argument("--v2-data-root", default=os.environ.get("NEMSEI_V2_HOST_DATA_ROOT", ""))
    parser.add_argument("--database", default=os.environ.get("NEMSEI_V2_DATABASE_PATH", ""))
    parser.add_argument("--compose-mount-source", default="")
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.v2.yml"))
    args = parser.parse_args()
    if not args.v1_data_root or not args.v2_data_root or not args.database:
        print("V1 root, V2 root, and V2 database path are required.", file=sys.stderr)
        return 1
    v1_root = Path(args.v1_data_root).expanduser().resolve()
    v2_root = Path(args.v2_data_root).expanduser().resolve()
    database = Path(args.database).expanduser().resolve()
    if v1_root == v2_root or contains(v1_root, v2_root) or contains(v2_root, v1_root):
        print("V1 and V2 data roots must be physically disjoint.", file=sys.stderr)
        return 1
    if database.name != V2_DATABASE_FILENAME or not contains(v2_root, database):
        print("V2 database must be nemsei_v2.db inside the V2 data root.", file=sys.stderr)
        return 1
    if args.compose_mount_source and Path(args.compose_mount_source).expanduser().resolve() != v2_root:
        print("Compose data mount source must be the validated V2 root.", file=sys.stderr)
        return 1
    compose_text = args.compose_file.read_text(encoding="utf-8")
    if "${NEMSEI_V2_HOST_DATA_ROOT:?" not in compose_text:
        print("Compose must mount NEMSEI_V2_HOST_DATA_ROOT explicitly.", file=sys.stderr)
        return 1
    print(f"V2 runtime isolation verified: {v2_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
