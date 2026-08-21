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
        # M7 Fatia 3: off by default (`device_status_poll_enabled=False`),
        # and even when on, restricted to exactly the one connection id
        # configured -- never a loop over every FusionSolar connection.
        if self.settings.device_status_poll_enabled and self.settings.device_status_poll_connection_id is not None:
            _device_job, device_created = self.repository.enqueue_due_device_status_poll(
                connection_id=self.settings.device_status_poll_connection_id,
                interval_minutes=self.settings.device_status_poll_interval_minutes,
                max_cycles=self.settings.device_status_poll_max_cycles,
            )
            created = created or device_created
        # D1: off by default, no connection id or cap needed -- this evaluates
        # every asset from already-persisted facts, never calls a provider.
        if self.settings.diagnostic_incident_evaluation_enabled:
            _incident_job, incident_created = self.repository.enqueue_due_incident_evaluation(
                interval_minutes=self.settings.diagnostic_incident_evaluation_interval_minutes,
            )
            created = created or incident_created
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
