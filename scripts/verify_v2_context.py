#!/usr/bin/env python3
"""Refuse V2 implementation/deployment work outside its linked worktree."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-branch", default="rewrite/v2")
    parser.add_argument("--forbid-root", type=Path)
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip()).resolve()
    branch = git("branch", "--show-current", cwd=root).strip()
    worktrees = git("worktree", "list", "--porcelain", cwd=root)
    expected_stanza = f"worktree {root}\n"
    if branch != args.expected_branch or expected_stanza not in worktrees or f"branch refs/heads/{args.expected_branch}" not in worktrees.split(expected_stanza, 1)[1].split("\n\n", 1)[0]:
        print("V2 context check failed: current linked worktree/branch is not rewrite/v2.", file=sys.stderr)
        return 1
    if args.forbid_root and root == args.forbid_root.resolve():
        print("V2 context check failed: current root is explicitly forbidden.", file=sys.stderr)
        return 1
    print(f"V2 context verified: {branch} at {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
