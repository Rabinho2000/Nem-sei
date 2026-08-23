"""Persisted request-control wrapper shared by FusionSolar read capabilities."""
from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nemsei.integrations.fusionsolar import v1_ownership
from nemsei.integrations.fusionsolar.client import FusionSolarClientError
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.sync.models import ProviderRequestAttempt, ProviderRequestState
from nemsei.sync.service import record_request_result, reserve_request


T = TypeVar("T")

# Mandatory by default while V1 is active, per the shared-account design in
# docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md -- every real HTTP call this
# controller makes must hold V1's account lease first. This exists only as
# an explicit, loud escape hatch for a future point where V1 no longer runs
# FusionSolar at all; it is not a performance knob and must not be set to
# skip the check for convenience while V1 is still live.
_SKIP_V1_OWNERSHIP_CHECK = os.environ.get("NEMSEI_V2_SKIP_V1_OWNERSHIP_CHECK", "").strip().lower() in {"1", "true", "yes"}


class FusionSolarRequestController:
    """Commits request evidence before HTTP and the result after HTTP."""

    def __init__(self, session_factory: sessionmaker[Session], *, max_transient_retries: int = 1) -> None:
        self._sessions = session_factory
        self._max_transient_retries = max(0, max_transient_retries)

    def call(
        self,
        *,
        connection_id: int,
        sync_run_id: int,
        endpoint_family: str,
        purpose: str,
        operation: Callable[[], T],
    ) -> tuple[T | None, ProviderError | None]:
        for _retry in range(self._max_transient_retries + 1):
            with self._sessions() as session:
                state, attempt, allowed = reserve_request(
                    session,
                    provider_connection_id=connection_id,
                    endpoint_family=endpoint_family,
                    purpose=purpose,
                    sync_run_id=sync_run_id,
                )
                session.commit()
                state_id, attempt_id = state.id, attempt.id
            if not allowed:
                return None, ProviderError(
                    ProviderErrorCode.RATE_LIMITED,
                    "FusionSolar request is deferred by persisted provider state.",
                    transient=True,
                )
            v1_lease = (
                v1_ownership.lease_for(
                    session_factory=self._sessions,
                    connection_id=connection_id,
                    owner=f"nemsei-v2-req-{attempt_id}",
                )
                if not _SKIP_V1_OWNERSHIP_CHECK
                else contextlib.nullcontext()
            )
            try:
                with v1_lease:
                    value = operation()
                error = None
            except v1_ownership.V1LeaseUnavailable as exc:
                self._finalize(
                    state_id,
                    attempt_id,
                    ProviderError(ProviderErrorCode.RATE_LIMITED, f"V1 ownership unavailable: {exc}", transient=True),
                )
                return None, ProviderError(ProviderErrorCode.RATE_LIMITED, f"V1 ownership unavailable: {exc}", transient=True)
            except FusionSolarClientError as exc:
                value, error = None, exc.error
            except Exception:
                # Preserve a truthful, sanitized terminal audit record even for
                # programming/transport surprises, then propagate the original.
                try:
                    self._finalize(
                        state_id,
                        attempt_id,
                        ProviderError(
                            ProviderErrorCode.UNKNOWN,
                            "Unexpected internal failure while invoking FusionSolar.",
                        ),
                    )
                except Exception:
                    # The original operation failure remains more actionable;
                    # never retry the provider call to repair local evidence.
                    pass
                raise
            self._finalize(state_id, attempt_id, error)
            if error is None or not error.transient or error.code is ProviderErrorCode.RATE_LIMITED:
                return value, error
        return None, error

    def _finalize(self, state_id: int, attempt_id: int, error: ProviderError | None) -> None:
        with self._sessions() as session:
            state = session.scalar(
                select(ProviderRequestState)
                .where(ProviderRequestState.id == state_id)
                .with_for_update()
            )
            attempt = session.get(ProviderRequestAttempt, attempt_id)
            assert state is not None and attempt is not None
            record_request_result(session, state=state, attempt=attempt, error=error)
            session.commit()
