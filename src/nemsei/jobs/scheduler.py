from __future__ import annotations

import signal
import time
import uuid

from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.repository import JobRepository


class Scheduler:
    def __init__(self, settings: Settings, *, owner_token: str | None = None) -> None:
        self.settings = settings.validate()
        engine = build_engine(settings)
        self.repository = JobRepository(engine, build_session_factory(engine))
        self.owner_token = owner_token or f"scheduler-{uuid.uuid4()}"
        self.stopping = False

    def stop(self, *_args) -> None:
        self.stopping = True

    def run_once(self) -> bool:
        if not self.repository.acquire_scheduler_lease(owner_token=self.owner_token, lease_seconds=self.settings.scheduler_lease_seconds):
            return False
        _job, created = self.repository.enqueue_due_noop()
        return created

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self.stopping:
            self.run_once()
            time.sleep(self.settings.worker_poll_seconds)


def main() -> None:
    Scheduler(Settings.from_environment()).run_forever()


if __name__ == "__main__":
    main()
