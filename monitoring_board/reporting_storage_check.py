from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from monitoring_board.reporting_storage import reconcile_generated_reports
from monitoring_board.runtime import DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Check generated reporting storage consistency.")
    parser.add_argument("--database", default=str(DB_PATH))
    parser.add_argument("--root", help="Generated reports root. Defaults to DATA_DIR/uploads/generated_reports.")
    parser.add_argument("--cleanup", action="store_true", help="Remove orphan files and stale staging files.")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not delete files.")
    args = parser.parse_args()
    cleanup = bool(args.cleanup and not args.dry_run)
    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    try:
        findings = reconcile_generated_reports(conn, root=Path(args.root) if args.root else None, cleanup=cleanup)
    finally:
        conn.close()
    for finding in findings:
        print(f"{finding.status}\t{finding.file_id or ''}\t{finding.run_id or ''}\t{finding.path}\t{finding.detail}")
    return 1 if any(finding.status not in {"ok"} for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
