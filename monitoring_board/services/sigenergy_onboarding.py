from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from monitoring_board.repositories import sigenergy as repository
from monitoring_board.services.sigenergy_contracts import (
    OPERATION_ONBOARDING,
    OnboardingResult,
    OnboardingStatus,
    validate_sigenergy_system_id,
)
from monitoring_board.services.sigenergy_models import (
    sanitize_payload,
    sanitize_sigenergy_error,
)


SubmitOnboarding = Callable[[str], dict[str, Any]]

ACTIVE_ONBOARDING_STATUSES = {
    OnboardingStatus.REQUESTED.value,
    OnboardingStatus.PROVIDER_PENDING.value,
    # Schema-compatible reads for responses stored by older releases.
    "already_requested",
    "already_requested_or_onboarded",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SigenergyOnboardingService:
    """Explicit provider-onboarding workflow, independent from direct access."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        submit: SubmitOnboarding,
        now: Callable[[], str] = _now,
    ) -> None:
        self.conn = conn
        self.submit = submit
        self.now = now
        repository.ensure_sigenergy_repository_schema(conn)

    def active_for_import(
        self,
        import_id: int,
    ) -> sqlite3.Row | None:
        placeholders = ", ".join(
            "?" for _ in ACTIVE_ONBOARDING_STATUSES
        )
        return self.conn.execute(
            f"""
            SELECT request.*
            FROM sigenergy_onboarding_requests request
            JOIN installation_imports import_row ON import_row.id = ?
            WHERE (
                    request.installation_import_id = import_row.id
                    OR request.id = import_row.onboarding_request_id
                  )
              AND request.status IN ({placeholders})
            ORDER BY request.id DESC
            LIMIT 1
            """,
            (import_id, *sorted(ACTIVE_ONBOARDING_STATUSES)),
        ).fetchone()

    def request_access(
        self,
        system_id: str,
        *,
        requested_by: str = "",
        installation_import_id: int | None = None,
    ) -> OnboardingResult:
        external_id = validate_sigenergy_system_id(system_id)
        existing = self._active_for_system(external_id)
        attempted_at = self.now()
        if existing is not None:
            if installation_import_id and not existing["installation_import_id"]:
                self.conn.execute(
                    """
                    UPDATE sigenergy_onboarding_requests
                    SET installation_import_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        installation_import_id,
                        attempted_at,
                        existing["id"],
                    ),
                )
            status = _stored_status(existing["status"])
            repository.record_operation_result(
                self.conn,
                operation=OPERATION_ONBOARDING,
                external_id=external_id,
                status=status.value,
                occurred_at=attempted_at,
                metadata={
                    "request_id": int(existing["id"]),
                    "reused": True,
                },
                succeeded=status is not OnboardingStatus.FAILED,
            )
            return OnboardingResult(
                external_id,
                status,
                attempted_at,
                int(existing["id"]),
                provider_code=str(existing["provider_code"] or ""),
                message=str(
                    existing["provider_message"]
                    or "O pedido de acesso ja estava pendente."
                ),
                reused=True,
            )

        raw_result = self.submit(external_id)
        if not isinstance(raw_result, dict):
            raw_result = {
                "status": OnboardingStatus.FAILED.value,
                "message": "O onboarding Sigenergy devolveu um resultado invalido.",
            }
        result = sanitize_payload(raw_result)
        status = _provider_status(result.get("status"))
        request_id = self.record_provider_result(
            external_id,
            requested_by=requested_by,
            result=result,
            installation_import_id=installation_import_id,
            attempted_at=attempted_at,
        )
        return OnboardingResult(
            external_id,
            status,
            attempted_at,
            request_id,
            provider_code=str(result.get("provider_code") or ""),
            message=sanitize_sigenergy_error(result.get("message") or ""),
        )

    def record_provider_result(
        self,
        system_id: str,
        *,
        requested_by: str,
        result: dict[str, Any],
        installation_import_id: int | None = None,
        attempted_at: str | None = None,
    ) -> int:
        external_id = validate_sigenergy_system_id(system_id)
        timestamp = attempted_at or self.now()
        safe_result = sanitize_payload(result)
        status = _provider_status(safe_result.get("status"))
        provider_code = str(safe_result.get("provider_code") or "")
        message = sanitize_sigenergy_error(
            safe_result.get("message") or ""
        )
        response_json = json.dumps(
            sanitize_payload(safe_result.get("response") or safe_result),
            ensure_ascii=True,
            sort_keys=True,
        )
        existing = self._active_for_system(external_id)
        if existing is None:
            cursor = self.conn.execute(
                """
                INSERT INTO sigenergy_onboarding_requests (
                    system_id, requested_at, requested_by, status,
                    provider_code, provider_message, last_checked_at,
                    approved_at, attempt_count, last_error, response_json,
                    created_at, updated_at, installation_import_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?, ?, ?, ?
                )
                """,
                (
                    external_id,
                    timestamp,
                    requested_by,
                    status.value,
                    provider_code,
                    message,
                    message
                    if status is OnboardingStatus.FAILED
                    else "",
                    response_json,
                    timestamp,
                    timestamp,
                    installation_import_id,
                ),
            )
            request_id = int(cursor.lastrowid)
        else:
            request_id = int(existing["id"])
            self.conn.execute(
                """
                UPDATE sigenergy_onboarding_requests
                SET status = ?, provider_code = ?, provider_message = ?,
                    attempt_count = attempt_count + 1, last_error = ?,
                    response_json = ?,
                    installation_import_id = COALESCE(
                        installation_import_id,
                        ?
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    provider_code,
                    message,
                    message
                    if status is OnboardingStatus.FAILED
                    else "",
                    response_json,
                    installation_import_id,
                    timestamp,
                    request_id,
                ),
            )
        repository.record_operation_result(
            self.conn,
            operation=OPERATION_ONBOARDING,
            external_id=external_id,
            status=status.value,
            occurred_at=timestamp,
            message=message,
            api_code=provider_code,
            metadata={
                "request_id": request_id,
                "installation_import_id": installation_import_id,
                "remote_request": True,
            },
            succeeded=status is not OnboardingStatus.FAILED,
        )
        return request_id

    def _active_for_system(
        self,
        external_id: str,
    ) -> sqlite3.Row | None:
        placeholders = ", ".join(
            "?" for _ in ACTIVE_ONBOARDING_STATUSES
        )
        return self.conn.execute(
            f"""
            SELECT *
            FROM sigenergy_onboarding_requests
            WHERE LOWER(system_id) = LOWER(?)
              AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (external_id, *sorted(ACTIVE_ONBOARDING_STATUSES)),
        ).fetchone()


def _provider_status(value: Any) -> OnboardingStatus:
    normalized = str(value or "").strip()
    try:
        return OnboardingStatus(normalized)
    except ValueError:
        if normalized in {
            "already_requested",
            "already_requested_or_onboarded",
        }:
            return OnboardingStatus.PROVIDER_PENDING
        return OnboardingStatus.FAILED


def _stored_status(value: Any) -> OnboardingStatus:
    return _provider_status(value)
