from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from monitoring_board.repositories import sigenergy as repository
from monitoring_board.services.energy_facts import (
    parse_sigenergy_daily_history,
    persist_sigenergy_daily_history,
)
from monitoring_board.services.sigenergy_contracts import (
    BackfillDayResult,
    BackfillDayStatus,
    BackfillPlanResult,
    DataQuality,
    FailureCategory,
    HistoryResult,
    OPERATION_HISTORY,
    SyncStatus,
    classify_sigenergy_failure,
    scoped_error,
    validate_sigenergy_system_id,
)
from monitoring_board.services.sigenergy_models import sanitize_payload
from monitoring_board.services.production_api_queue import (
    ApiSlotUnavailableError,
)


SigenergyCall = Callable[[str, int, Callable[[], Any]], Any]
EnqueueHistoryDay = Callable[[str, date], tuple[int, bool]]


def _direct_call(_area: str, _priority: int, callback: Callable[[], Any]) -> Any:
    return callback()


class SigenergyHistoryService:
    """Direct history ingestion for one mapped Sigenergy System ID."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        client: Any,
        confirmed_unit: str,
        execute_call: SigenergyCall | None = None,
        now: Callable[[], str] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.conn = conn
        self.client = client
        self.confirmed_unit = confirmed_unit
        self.execute_call = execute_call or _direct_call
        self.now = now or (
            lambda: datetime.now().isoformat(timespec="seconds")
        )
        self.today = today or date.today
        repository.ensure_sigenergy_repository_schema(conn)

    def sync_day(
        self,
        system_id: str,
        target_date: date,
    ) -> HistoryResult:
        external_id = validate_sigenergy_system_id(system_id)
        attempted_at = self.now()
        if target_date >= self.today():
            raise ValueError(
                "A energia Sigenergy diaria exige um dia ja terminado."
            )
        mapping = repository.mapping_for_system(self.conn, external_id)
        if mapping is None:
            raise ValueError(
                "A instalacao Sigenergy tem de estar associada e ativa "
                "antes do historico."
            )
        repository.ensure_mapped_inventory(
            self.conn,
            external_id=external_id,
            external_name=str(
                mapping.get("external_name")
                or mapping.get("project_name")
                or external_id
            ),
            observed_at=attempted_at,
        )
        try:
            history = self.execute_call(
                OPERATION_HISTORY,
                3,
                lambda: self.client.get_system_history(
                    external_id,
                    level="Day",
                    target_date=target_date.isoformat(),
                ),
            )
            if not isinstance(history, dict):
                raise ValueError(
                    "O historico Sigenergy devolveu um payload invalido."
                )
            fact = parse_sigenergy_daily_history(
                sanitize_payload(history),
                system_id=external_id,
                period_date=target_date,
                confirmed_unit=self.confirmed_unit,
            )
            fact_id = persist_sigenergy_daily_history(
                self.conn,
                asset_id=int(mapping["asset_id"]),
                fact=fact,
            )
        except ApiSlotUnavailableError:
            raise
        except Exception as exc:
            failure = classify_sigenergy_failure(
                exc,
                operation=OPERATION_HISTORY,
            )
            status = (
                SyncStatus.RATE_LIMITED
                if failure.category is FailureCategory.RATE_LIMITED
                else SyncStatus.FAILED
            )
            error = scoped_error(
                failure,
                operation=OPERATION_HISTORY,
                external_id=external_id,
                occurred_at=attempted_at,
            )
            repository.record_scoped_error(
                self.conn,
                error,
                metadata={"target_date": target_date.isoformat()},
                state_status=status.value,
            )
            return HistoryResult(
                external_id,
                target_date.isoformat(),
                status,
                attempted_at,
                asset_id=int(mapping["asset_id"]),
                data_quality=(
                    DataQuality.INVALID
                    if failure.category is FailureCategory.PROVIDER_ERROR
                    else DataQuality.MISSING
                ),
                error=error,
                cooldown_until=failure.cooldown_until,
            )

        quality = DataQuality(fact.data_quality)
        repository.set_inventory_data_quality(
            self.conn,
            external_id=external_id,
            data_quality=quality,
            observed_at=attempted_at,
        )
        repository.record_operation_result(
            self.conn,
            operation=OPERATION_HISTORY,
            external_id=external_id,
            status=SyncStatus.SUCCESS.value,
            occurred_at=attempted_at,
            metadata={
                "target_date": target_date.isoformat(),
                "fact_id": fact_id,
                "data_quality": quality.value,
            },
            succeeded=True,
        )
        return HistoryResult(
            external_id,
            target_date.isoformat(),
            SyncStatus.SUCCESS,
            attempted_at,
            asset_id=int(mapping["asset_id"]),
            fact_id=fact_id,
            data_quality=quality,
        )


class SigenergyBackfillService:
    """Plan one idempotent background job per incomplete historical day."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        enqueue_day: EnqueueHistoryDay,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.conn = conn
        self.enqueue_day = enqueue_day
        self.today = today or date.today
        repository.ensure_sigenergy_repository_schema(conn)

    def plan(
        self,
        system_id: str,
        *,
        date_from: date,
        date_to: date,
        max_days: int = 31,
    ) -> BackfillPlanResult:
        external_id = validate_sigenergy_system_id(system_id)
        if date_from > date_to:
            raise ValueError(
                "O inicio do backfill nao pode ser posterior ao fim."
            )
        if date_to >= self.today():
            raise ValueError(
                "O historico Sigenergy so pode pedir dias ja terminados."
            )
        day_count = (date_to - date_from).days + 1
        if day_count > max_days:
            raise ValueError(
                "O backfill Sigenergy esta limitado a "
                f"{max_days} dias por pedido."
            )
        mapping = repository.mapping_for_system(self.conn, external_id)
        if mapping is None:
            raise ValueError(
                "A instalacao Sigenergy tem de estar associada e ativa "
                "antes do backfill."
            )
        asset_id = int(mapping["asset_id"])
        days: list[BackfillDayResult] = []
        for offset in range(day_count):
            target_date = date_from + timedelta(days=offset)
            target_date_text = target_date.isoformat()
            if repository.history_day_is_complete(
                self.conn,
                asset_id=asset_id,
                external_id=external_id,
                target_date=target_date_text,
            ):
                days.append(
                    BackfillDayResult(
                        target_date_text,
                        BackfillDayStatus.COMPLETE,
                    )
                )
                continue
            job_id, created = self.enqueue_day(external_id, target_date)
            days.append(
                BackfillDayResult(
                    target_date_text,
                    (
                        BackfillDayStatus.QUEUED
                        if created
                        else BackfillDayStatus.REUSED
                    ),
                    job_id=job_id,
                )
            )
        return BackfillPlanResult(
            external_id,
            asset_id,
            date_from.isoformat(),
            date_to.isoformat(),
            tuple(days),
        )
