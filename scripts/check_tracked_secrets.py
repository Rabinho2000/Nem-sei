from __future__ import annotations

import re
import subprocess
from pathlib import Path


PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
)
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".xlsx", ".xls", ".zip"}


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    findings: list[str] = []
    for raw_path in tracked:
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        if path.suffix.casefold() in SKIP_SUFFIXES or not path.is_file():
            continue
        data = path.read_bytes()
        if any(pattern.search(data) for pattern in PATTERNS):
            findings.append(str(path))
    if findings:
        print("Possível secret em ficheiro tracked (conteúdo omitido):")
        for path in findings:
            print(f"- {path}")
        return 1
    print(f"Scanner simples: {len(tracked) - 1} ficheiros tracked verificados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
