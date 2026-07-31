from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from monitoring_board.repositories import sigenergy as repository
from monitoring_board.services.sigenergy_contracts import (
    AccessStatus,
    MappingStatus,
    OPERATION_MAPPING,
    validate_sigenergy_system_id,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SigenergyMappingService:
    """Manage one Sigenergy-to-asset mapping without remote side effects."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now: Callable[[], str] = _now,
    ) -> None:
        self.conn = conn
        self.now = now
        repository.ensure_sigenergy_repository_schema(conn)

    def map_system(
        self,
        *,
        external_id: str,
        asset_id: int | None,
        actor: str = "",
    ) -> MappingStatus:
        system_id = validate_sigenergy_system_id(external_id)
        existing = repository.mapping_for_system(
            self.conn,
            system_id,
            require_enabled=False,
        )
        occurred_at = self.now()
        previous_asset_id = (
            int(existing["asset_id"]) if existing is not None else None
        )
        if asset_id is None:
            self.conn.execute(
                """
                DELETE FROM asset_integrations
                WHERE provider = 'Sigenergy' AND external_id = ?
                """,
                (system_id,),
            )
            repository.record_operation_result(
                self.conn,
                operation=OPERATION_MAPPING,
                external_id=system_id,
                status=MappingStatus.UNASSOCIATED.value,
                occurred_at=occurred_at,
                metadata={
                    "actor": actor,
                    "previous_asset_id": previous_asset_id,
                    "asset_id": None,
                },
                succeeded=True,
            )
            return MappingStatus.UNASSOCIATED

        inventory = repository.inventory_identity(self.conn, system_id)
        if inventory is None:
            raise ValueError(
                "O sistema Sigenergy nao existe no inventario local."
            )
        if str(inventory.get("access_status") or "") != (
            AccessStatus.ACCESSIBLE.value
        ):
            raise ValueError(
                "O acesso ao System ID tem de ser confirmado antes da "
                "associacao."
            )

        asset = self.conn.execute(
            "SELECT id FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        if asset is None:
            raise ValueError("O asset local escolhido nao existe.")
        self.conn.execute(
            """
            INSERT INTO asset_integrations (
                asset_id, provider, external_id, external_name, enabled,
                is_primary_energy_source, last_sync_at, last_status,
                last_error
            ) VALUES (?, 'Sigenergy', ?, ?, 1, 0, NULL, NULL, '')
            ON CONFLICT(provider, external_id) DO UPDATE SET
                asset_id = excluded.asset_id,
                external_name = excluded.external_name,
                enabled = 1,
                is_primary_energy_source = CASE
                    WHEN asset_integrations.asset_id = excluded.asset_id
                        THEN asset_integrations.is_primary_energy_source
                    ELSE 0
                END,
                last_error = ''
            """,
            (
                asset_id,
                system_id,
                str(inventory.get("external_name") or system_id),
            ),
        )
        repository.record_operation_result(
            self.conn,
            operation=OPERATION_MAPPING,
            external_id=system_id,
            status=MappingStatus.ASSOCIATED.value,
            occurred_at=occurred_at,
            metadata={
                "actor": actor,
                "previous_asset_id": previous_asset_id,
                "asset_id": asset_id,
                "changed": previous_asset_id != asset_id,
            },
            succeeded=True,
        )
        return MappingStatus.ASSOCIATED


def map_sigenergy_system(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    asset_id: int | None,
    actor: str = "",
) -> MappingStatus:
    return SigenergyMappingService(conn).map_system(
        external_id=external_id,
        asset_id=asset_id,
        actor=actor,
    )
