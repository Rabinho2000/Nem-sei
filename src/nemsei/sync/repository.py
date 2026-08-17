"""Persistence-only lookups for sync-control services."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.sync.models import IntegrationHealth, ProviderRequestState, SyncCursor


class SyncRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def health(self, provider_connection_id: int) -> IntegrationHealth | None:
        return self.session.get(IntegrationHealth, provider_connection_id)

    def cursor(self, *, provider_connection_id: int, capability: str, cursor_key: str) -> SyncCursor | None:
        return self.session.scalar(select(SyncCursor).where(SyncCursor.provider_connection_id == provider_connection_id, SyncCursor.capability == capability, SyncCursor.cursor_key == cursor_key))

    def request_state(self, *, provider_connection_id: int, endpoint_family: str) -> ProviderRequestState | None:
        return self.session.scalar(select(ProviderRequestState).where(ProviderRequestState.provider_connection_id == provider_connection_id, ProviderRequestState.endpoint_family == endpoint_family))
