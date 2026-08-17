#!/usr/bin/env python3
"""Validate that deployment paths physically isolate V2 runtime data."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


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
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.v2.yml"))
    args = parser.parse_args()
    if not args.v1_data_root or not args.v2_data_root:
        print("V1 and V2 runtime roots are required.", file=sys.stderr)
        return 1
    v1_root = Path(args.v1_data_root).expanduser().resolve()
    v2_root = Path(args.v2_data_root).expanduser().resolve()
    if v1_root == v2_root or contains(v1_root, v2_root) or contains(v2_root, v1_root):
        print("V1 and V2 data roots must be physically disjoint.", file=sys.stderr)
        return 1
    compose_text = args.compose_file.read_text(encoding="utf-8")
    if "Nem-sei/data" in compose_text or "monitoring_board.db" in compose_text:
        print("V2 Compose must not reference V1 runtime data.", file=sys.stderr)
        return 1
    print(f"V2 runtime isolation verified: {v2_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
