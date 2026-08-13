from __future__ import annotations

import signal
import time
import uuid
from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.handlers import RetryableJobError, execute
from nemsei.jobs.repository import JobRepository


class Worker:
    def __init__(self, settings: Settings, *, worker_id: str | None = None) -> None:
        self.settings = settings.validate()
        engine = build_engine(settings)
        self.repository = JobRepository(engine, build_session_factory(engine))
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.stopping = False

    def stop(self, *_args) -> None:
        self.stopping = True

    def run_once(self) -> bool:
        self.repository.recover_expired()
        self.repository.activate_due_waiting()
        claimed = self.repository.claim_next(worker_id=self.worker_id, lease_seconds=self.settings.worker_lease_seconds)
        if claimed is None:
            return False
        try:
            outcome = execute(claimed, testing=self.settings.testing)
            self.repository.finish(claimed, status=outcome.status, result=outcome.result)
        except RetryableJobError as exc:
            delay = 60 if claimed.attempt == 1 else 300
            self.repository.retry_or_fail(claimed, error_type=type(exc).__name__, message=str(exc), delay_seconds=delay)
        except Exception as exc:
            self.repository.retry_or_fail(claimed, error_type=type(exc).__name__, message=str(exc), delay_seconds=60)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self.stopping:
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)


def main() -> None:
    Worker(Settings.from_environment()).run_forever()


if __name__ == "__main__":
    main()
