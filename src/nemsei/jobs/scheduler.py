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
        # Off by default, and even when on, restricted to exactly the one
        # connection id configured -- never a loop over every FusionSolar
        # connection (docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md's rollout
        # write-up: this is a shared, rate-limited account, so scaling here
        # needs a deliberate second call site, not a config flip).
        if self.settings.production_sync_scheduler_enabled and self.settings.production_sync_scheduler_connection_id is not None:
            _production_job, production_created = self.repository.enqueue_due_production_incremental(
                connection_id=self.settings.production_sync_scheduler_connection_id,
                interval_hours=self.settings.production_sync_scheduler_interval_hours,
            )
            created = created or production_created
        # Sigenergy, same shape and same restraint: off by default, one
        # explicit connection, never a loop over every Sigenergy connection.
        if self.settings.sigenergy_sync_scheduler_enabled and self.settings.sigenergy_sync_scheduler_connection_id is not None:
            _sigen_job, sigen_created = self.repository.enqueue_due_production_incremental(
                connection_id=self.settings.sigenergy_sync_scheduler_connection_id,
                interval_hours=self.settings.sigenergy_sync_scheduler_interval_hours,
            )
            created = created or sigen_created
        # Plant state, per provider account. One call per 100 plants, on its
        # own endpoint-family budget -- but still off by default and still one
        # explicit connection each, because it is a provider call and the
        # rule here has never been "loop over what happens to be configured".
        for enabled, connection_id, interval in (
            (self.settings.current_monitoring_scheduler_enabled, self.settings.current_monitoring_scheduler_connection_id, self.settings.current_monitoring_scheduler_interval_minutes),
            (self.settings.sigenergy_current_monitoring_scheduler_enabled, self.settings.sigenergy_current_monitoring_scheduler_connection_id, self.settings.sigenergy_current_monitoring_scheduler_interval_minutes),
        ):
            if enabled and connection_id is not None:
                _state_job, state_created = self.repository.enqueue_due_current_monitoring(
                    connection_id=connection_id,
                    interval_minutes=interval,
                )
                created = created or state_created
        # Crash recovery for sync runs, on by default: zero provider calls, and
        # the only rows it touches are ones whose owning process is provably
        # gone. See sync/abandonment.py.
        if self.settings.sync_run_sweep_enabled:
            _sweep_job, sweep_created = self.repository.enqueue_due_sync_run_sweep(
                interval_minutes=self.settings.sync_run_sweep_interval_minutes,
            )
            created = created or sweep_created
        # D1: off by default, no connection id or cap needed -- this evaluates
        # every asset from already-persisted facts, never calls a provider.
        if self.settings.diagnostic_incident_evaluation_enabled:
            _incident_job, incident_created = self.repository.enqueue_due_incident_evaluation(
                interval_minutes=self.settings.diagnostic_incident_evaluation_interval_minutes,
            )
            created = created or incident_created
        # Report finalisation: off by default, and provider-free like the
        # incident evaluator above. It only ever adds a snapshot beside a
        # provisional one; it cannot rewrite a report and cannot approve a
        # portfolio run, both of which stay an operator's act.
        if self.settings.report_month_close_enabled:
            _close_job, close_created = self.repository.enqueue_due_report_month_close(
                interval_minutes=self.settings.report_month_close_interval_minutes,
            )
            created = created or close_created
        # D3: off by default. This switch decides whether the pass *runs*;
        # whether it may deliver is the separate global `NEMSEI_V2_NOTIFICATIONS`
        # capability, checked in notifications/service.py. Since D4 the client
        # is a real one whenever a bot token is mounted, so the two are not
        # interchangeable.
        if self.settings.notification_processing_enabled:
            _notification_job, notification_created = self.repository.enqueue_due_notification_processing(
                interval_minutes=self.settings.notification_processing_interval_minutes,
            )
            created = created or notification_created
        # D6: off by default, daily by default. A summary over an already-
        # closed window, never an immediate alert -- and generation itself
        # makes no provider call either.
        if self.settings.digest_generation_enabled:
            _digest_job, digest_created = self.repository.enqueue_due_digest_generation(
                interval_minutes=self.settings.digest_generation_interval_minutes,
            )
            created = created or digest_created
        # Telegram O&M redesign, Fatia 4 (req 13): grouped recoveries, off by
        # default, same content-digest reasoning as the diagnostics digest
        # above -- no provider call, reads notification_episodes only.
        if self.settings.recovery_digest_generation_enabled:
            _recovery_job, recovery_created = self.repository.enqueue_due_recovery_digest(
                interval_minutes=self.settings.recovery_digest_interval_minutes,
            )
            created = created or recovery_created
        # Reqs 10-11: the daily O&M briefing, off by default. No provider
        # call; reads notification_episodes/contracts/work_orders/contacts,
        # all already-persisted.
        if self.settings.morning_briefing_enabled:
            _briefing_job, briefing_created = self.repository.enqueue_due_morning_briefing(
                interval_minutes=self.settings.morning_briefing_interval_minutes,
                hour=self.settings.morning_briefing_hour,
                minute=self.settings.morning_briefing_minute,
                tz_name=self.settings.morning_briefing_timezone,
            )
            created = created or briefing_created
        # Huawei SCADA. Both are pure database work over samples a separate
        # listener process already collected -- no provider call, no call
        # budget, and nothing here can start or stop the listener itself.
        if self.settings.huawei_scada_rollup_enabled and self.settings.huawei_scada_rollup_connection_id is not None:
            _rollup_job, rollup_created = self.repository.enqueue_due_huawei_scada_rollup(
                connection_id=self.settings.huawei_scada_rollup_connection_id,
                interval_minutes=self.settings.huawei_scada_rollup_interval_minutes,
                lookback_days=self.settings.huawei_scada_rollup_lookback_days,
            )
            created = created or rollup_created
        # Retention rides on the rollup's connection id: deleting samples for a
        # connection nothing is rolling up would delete evidence that never
        # became energy.
        if (
            self.settings.huawei_scada_retention_enabled
            and self.settings.huawei_scada_rollup_enabled
            and self.settings.huawei_scada_rollup_connection_id is not None
        ):
            _retention_job, retention_created = self.repository.enqueue_due_huawei_scada_retention(
                connection_id=self.settings.huawei_scada_rollup_connection_id,
                interval_minutes=self.settings.huawei_scada_retention_interval_minutes,
                retention_days=self.settings.huawei_scada_retention_days,
            )
            created = created or retention_created
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
