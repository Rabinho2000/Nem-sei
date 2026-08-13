from __future__ import annotations

import argparse

from nemsei.jobs.scheduler import main as scheduler_main
from nemsei.jobs.worker import main as worker_main


def main() -> None:
    parser = argparse.ArgumentParser(description="Nem-sei V2 process launcher")
    parser.add_argument("role", choices=("worker", "scheduler"))
    role = parser.parse_args().role
    {"worker": worker_main, "scheduler": scheduler_main}[role]()


if __name__ == "__main__":
    main()
