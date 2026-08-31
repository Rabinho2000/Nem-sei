#!/usr/bin/env python3
"""Decide which PostgreSQL dumps to keep, and delete only the rest.

The previous rule was `find -mtime +6`: seven days and nothing older. That is
enough to survive a bad deploy and not enough to survive anything discovered
late -- a silent corruption, a column quietly written wrong, an import that
went in backwards. By the time a person notices, the last good copy has been
deleted on schedule.

So: the newest dump of each of the last 7 days, of each of the last 4 weeks,
and of each of the last 3 months. At the observed 47 MB a dump that is under a
gigabyte in total, against hundreds free.

The windows count *dumps that exist*, not wall-clock days. A server that was
off for a fortnight comes back with its history intact instead of having aged
everything out, and the rule can be tested without pretending to control the
clock. Anything this cannot parse is kept, always: deleting a backup because
its name was unfamiliar is the one outcome worth designing against.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

DAILY = 7
WEEKLY = 4
MONTHLY = 3

# `nemsei-v2-20260831T092641Z.dump`, written by v2_postgres_backup.sh.
STAMP = re.compile(r"^nemsei-v2-(\d{8}T\d{6}Z)\.dump$")


def parse_stamp(name: str) -> datetime | None:
    match = STAMP.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None


def retained(names, daily: int = DAILY, weekly: int = WEEKLY, monthly: int = MONTHLY) -> set[str]:
    """The newest dump of each of the last `daily` days, `weekly` weeks, `monthly` months."""
    dated = sorted(
        ((stamp, name) for name in names if (stamp := parse_stamp(name)) is not None),
        reverse=True,
    )
    keep: set[str] = set()
    buckets = (
        (lambda moment: moment.date(), daily),
        (lambda moment: moment.isocalendar()[:2], weekly),
        (lambda moment: (moment.year, moment.month), monthly),
    )
    for key, limit in buckets:
        seen: list = []
        for stamp, name in dated:
            bucket = key(stamp)
            if bucket in seen:
                continue
            seen.append(bucket)
            if len(seen) > limit:
                break
            keep.add(name)
    return keep


def expired(names, **limits) -> list[str]:
    """The dumps safe to delete. Unparseable names are never in here."""
    understood = {name for name in names if parse_stamp(name) is not None}
    return sorted(understood - retained(names, **limits))


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply backup retention to a directory.")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Remove the expired dumps. Without it, they are only listed.",
    )
    parser.add_argument("--daily", type=int, default=DAILY)
    parser.add_argument("--weekly", type=int, default=WEEKLY)
    parser.add_argument("--monthly", type=int, default=MONTHLY)
    args = parser.parse_args()

    names = [entry.name for entry in args.directory.iterdir() if entry.is_file()]
    for name in expired(names, daily=args.daily, weekly=args.weekly, monthly=args.monthly):
        path = args.directory / name
        if args.delete:
            path.unlink()
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
