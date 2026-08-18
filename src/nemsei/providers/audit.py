"""Small sanitized operator-audit service."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from nemsei.providers.models import OPERATOR_AUDIT_ACTIONS, OperatorAuditEvent
from nemsei.shared.clock import utc_now


_ALLOWED_METADATA = {
    "provider_code",
    "connection_id",
    "mapping_id",
    "asset_id",
    "source_use",
    "valid_from",
    "valid_to",
    "priority",
    "is_fallback",
    "finding_codes",
    "capability",
    "provider_call_count",
    "result_status",
}


def record_operator_action(
    session: Session,
    *,
    actor_username: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    metadata: dict[str, Any] | None = None,
) -> OperatorAuditEvent:
    if action not in OPERATOR_AUDIT_ACTIONS:
        raise ValueError("Unsupported operator audit action.")
    actor = (actor_username or "system").strip()[:120] or "system"
    safe_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if key in _ALLOWED_METADATA and isinstance(value, (str, int, bool, type(None), list, tuple))
    }
    event = OperatorAuditEvent(
        actor_username=actor,
        action=action,
        entity_type=entity_type[:64],
        entity_id=entity_id,
        metadata_json=safe_metadata,
        occurred_at=utc_now(),
    )
    session.add(event)
    session.flush()
    return event
