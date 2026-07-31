from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.repositories import sigenergy as repository
from monitoring_board.services.api_rate_limit import ApiRateLimitError
from monitoring_board.services.sigenergy_contracts import (
    AccessOutcome,
    AccessStatus,
    CredentialOutcome,
    CredentialStatus,
    DiscoveryStatus,
    OPERATION_ACCESS,
    OPERATION_CREDENTIALS,
    OPERATION_DISCOVERY,
    OPERATION_MAPPING,
    OPERATION_STATE_SYNC,
    SyncStatus,
)
from monitoring_board.services.sigenergy_errors import (
    SigenergyApiError,
    SigenergyAuthError,
)
from monitoring_board.services.sigenergy_mapping import (
    SigenergyMappingService,
)
from monitoring_board.services.sigenergy_onboarding import (
    SigenergyOnboardingService,
)
from monitoring_board.services.sigenergy_operations import (
    SigenergyIntegrationService,
)


NOW = "2026-07-30T18:00:00"


def _database(tmp_path, name: str):
    path = tmp_path / name
    app_module.ensure_database(str(path))
    return path


def _insert_asset_mapping(
    conn,
    external_id: str,
    *,
    enabled: int = 1,
    project_name: str | None = None,
) -> int:
    name = project_name or external_id
    asset_id = int(
        conn.execute(
            "INSERT INTO assets (project_name) VALUES (?)",
            (name,),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO asset_integrations (
            asset_id, provider, external_id, external_name, enabled
        ) VALUES (?, 'Sigenergy', ?, ?, ?)
        """,
        (asset_id, external_id, name, enabled),
    )
    return asset_id


class CredentialSpy:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def authenticate(self) -> str:
        self.calls.append("authenticate")
        if self.error is not None:
            raise self.error
        return "token"

    def list_systems(self, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("credential test called discovery")

    def get_energy_flow(self, _system_id: str) -> dict[str, Any]:
        raise AssertionError("credential test reused a System ID")


@pytest.mark.parametrize(
    ("error", "outcome", "status"),
    [
        (None, CredentialOutcome.AUTHENTICATED, CredentialStatus.VALID),
        (
            SigenergyAuthError("invalid", status_code=401),
            CredentialOutcome.AUTH_FAILED,
            CredentialStatus.INVALID,
        ),
        (
            SigenergyAuthError("forbidden", status_code=403),
            CredentialOutcome.AUTH_FAILED,
            CredentialStatus.INVALID,
        ),
        (
            ApiRateLimitError(
                "Sigenergy",
                "credentials",
                datetime.now() + timedelta(minutes=30),
                "limited",
            ),
            CredentialOutcome.RATE_LIMITED,
            CredentialStatus.RATE_LIMITED,
        ),
        (
            SigenergyApiError("provider down", status_code=500),
            CredentialOutcome.PROVIDER_ERROR,
            CredentialStatus.ERROR,
        ),
    ],
)
def test_credentials_have_no_discovery_or_system_side_effects(
    tmp_path,
    error,
    outcome,
    status,
) -> None:
    db_path = _database(tmp_path, "credentials.db")
    client = CredentialSpy(error)
    with get_db(str(db_path)) as conn:
        result = SigenergyIntegrationService(
            conn,
            client=client,
            now=lambda: NOW,
        ).test_credentials()
        inventory_count = conn.execute(
            "SELECT COUNT(*) FROM provider_system_inventory"
        ).fetchone()[0]
        side_effect_counts = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "assets",
                "asset_integrations",
                "installation_imports",
                "integration_realtime_snapshots",
                "sigenergy_access_validations",
                "sigenergy_onboarding_requests",
            )
        }
        state = repository.get_operation_state(
            conn,
            operation=OPERATION_CREDENTIALS,
        )

    assert result.outcome is outcome
    assert result.credential_status is status
    assert client.calls == ["authenticate"]
    assert inventory_count == 0
    assert not any(side_effect_counts.values())
    assert state["status"] == status.value


class DiscoveryClient:
    def __init__(self, response: Any = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def list_systems(self, *, allow_empty: bool = False):
        assert allow_empty is True
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize("code", [1201, "1201"])
def test_discovery_1201_is_restricted_and_changes_no_other_workflow(
    tmp_path,
    code,
) -> None:
    db_path = _database(tmp_path, f"discovery-{code}.db")
    with get_db(str(db_path)) as conn:
        asset_id = _insert_asset_mapping(conn, "KNOWN")
        repository.ensure_mapped_inventory(
            conn,
            external_id="KNOWN",
            external_name="Known system",
            observed_at=NOW,
        )
        conn.execute(
            """
            UPDATE provider_system_inventory
            SET access_status = 'accessible',
                validation_method = 'direct_energy_flow'
            WHERE external_id = 'KNOWN'
            """
        )
        onboarding = SigenergyOnboardingService(
            conn,
            submit=lambda system_id: {
                "status": "requested",
                "provider_code": "0",
                "message": "pending",
                "response": {"system_id": system_id},
            },
            now=lambda: NOW,
        ).request_access("KNOWN")
        result = SigenergyIntegrationService(
            conn,
            client=DiscoveryClient(
                error=SigenergyApiError(
                    "Access restriction",
                    api_code=code,
                )
            ),
            now=lambda: NOW,
        ).discover_systems()
        inventory = conn.execute(
            """
            SELECT access_status, validation_method
            FROM provider_system_inventory
            WHERE external_id = 'KNOWN'
            """
        ).fetchone()
        mapping = conn.execute(
            """
            SELECT asset_id, enabled
            FROM asset_integrations
            WHERE provider = 'Sigenergy' AND external_id = 'KNOWN'
            """
        ).fetchone()
        request = conn.execute(
            """
            SELECT status
            FROM sigenergy_onboarding_requests
            WHERE id = ?
            """,
            (onboarding.request_id,),
        ).fetchone()
        state = repository.get_operation_state(
            conn,
            operation=OPERATION_DISCOVERY,
        )

    assert result.status is DiscoveryStatus.RESTRICTED
    assert result.systems == ()
    assert inventory["access_status"] == AccessStatus.ACCESSIBLE.value
    assert inventory["validation_method"] == "direct_energy_flow"
    assert mapping["asset_id"] == asset_id
    assert mapping["enabled"] == 1
    assert request["status"] == "requested"
    assert state["status"] == DiscoveryStatus.RESTRICTED.value


@pytest.mark.parametrize(
    ("response", "error", "expected"),
    [
        ([], None, DiscoveryStatus.EMPTY),
        (
            [{"systemId": "NEW", "systemName": "New"}],
            None,
            DiscoveryStatus.SUCCESS,
        ),
        (
            None,
            SigenergyAuthError("unauthorized", status_code=401),
            DiscoveryStatus.ERROR,
        ),
        (
            None,
            SigenergyApiError("forbidden", status_code=403),
            DiscoveryStatus.ERROR,
        ),
        (
            None,
            SigenergyApiError("other code", api_code="9000"),
            DiscoveryStatus.ERROR,
        ),
        (
            None,
            ApiRateLimitError(
                "Sigenergy",
                "discovery",
                datetime.now() + timedelta(minutes=30),
                "limited",
            ),
            DiscoveryStatus.RATE_LIMITED,
        ),
        ("not-a-list", None, DiscoveryStatus.ERROR),
        ([{"systemName": "missing ID"}], None, DiscoveryStatus.ERROR),
        (["invalid row"], None, DiscoveryStatus.ERROR),
    ],
)
def test_discovery_explicit_outcomes(
    tmp_path,
    response,
    error,
    expected,
) -> None:
    db_path = _database(tmp_path, f"discovery-{expected.value}.db")
    with get_db(str(db_path)) as conn:
        result = SigenergyIntegrationService(
            conn,
            client=DiscoveryClient(response=response, error=error),
            now=lambda: NOW,
        ).discover_systems()

    assert result.status is expected


def test_empty_discovery_does_not_invalidate_credentials(tmp_path) -> None:
    db_path = _database(tmp_path, "empty-discovery.db")
    with get_db(str(db_path)) as conn:
        repository.record_operation_result(
            conn,
            operation=OPERATION_CREDENTIALS,
            status=CredentialStatus.VALID.value,
            occurred_at=NOW,
            metadata={"outcome": CredentialOutcome.AUTHENTICATED.value},
            succeeded=True,
        )
        result = SigenergyIntegrationService(
            conn,
            client=DiscoveryClient(response=[]),
            now=lambda: NOW,
        ).discover_systems()
        credential_state = repository.get_operation_state(
            conn,
            operation=OPERATION_CREDENTIALS,
        )

    assert result.status is DiscoveryStatus.EMPTY
    assert result.systems == ()
    assert result.station_count == 0
    assert credential_state["status"] == CredentialStatus.VALID.value


def test_discovery_does_not_delete_absent_systems(tmp_path) -> None:
    db_path = _database(tmp_path, "discovery-absence.db")
    with get_db(str(db_path)) as conn:
        repository.upsert_discovered_systems(
            conn,
            [{"systemId": "OLD", "systemName": "Old"}],
            discovered_at="2026-07-29T18:00:00",
        )
        result = SigenergyIntegrationService(
            conn,
            client=DiscoveryClient(
                response=[{"systemId": "NEW", "systemName": "New"}]
            ),
            now=lambda: NOW,
        ).discover_systems()
        inventory = {
            row["external_id"]: row["access_status"]
            for row in conn.execute(
                """
                SELECT external_id, access_status
                FROM provider_system_inventory
                ORDER BY external_id
                """
            )
        }

    assert result.status is DiscoveryStatus.SUCCESS
    assert inventory == {"NEW": "accessible", "OLD": "accessible"}


class AccessClient:
    def __init__(
        self,
        *,
        flow: Any = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.flow = flow if flow is not None else {"pvPower": 1.25}
        self.errors = errors or {}
        self.energy_calls: list[str] = []
        self.discovery_calls = 0

    def get_energy_flow(self, system_id: str):
        self.energy_calls.append(system_id)
        if system_id in self.errors:
            raise self.errors[system_id]
        return self.flow

    def list_systems(self, **_kwargs: Any):
        self.discovery_calls += 1
        raise AssertionError("direct access called discovery")


@pytest.mark.parametrize(
    ("error", "outcome", "access_status"),
    [
        (
            SigenergyAuthError("auth", status_code=401),
            AccessOutcome.AUTH_FAILED,
            AccessStatus.ERROR,
        ),
        (
            SigenergyApiError("forbidden", status_code=403),
            AccessOutcome.UNAUTHORIZED,
            AccessStatus.UNAUTHORIZED,
        ),
        (
            SigenergyApiError("missing", status_code=404),
            AccessOutcome.NOT_FOUND,
            AccessStatus.NOT_FOUND,
        ),
        (
            ApiRateLimitError(
                "Sigenergy",
                "state",
                datetime.now() + timedelta(minutes=30),
                "limited",
            ),
            AccessOutcome.RATE_LIMITED,
            AccessStatus.ERROR,
        ),
        (
            SigenergyApiError("provider", status_code=500),
            AccessOutcome.PROVIDER_ERROR,
            AccessStatus.ERROR,
        ),
    ],
)
def test_direct_access_failure_matrix_is_audited_without_local_creation(
    tmp_path,
    error,
    outcome,
    access_status,
) -> None:
    db_path = _database(tmp_path, f"access-{outcome.value}.db")
    client = AccessClient(errors={"SYSTEM-1": error})
    with get_db(str(db_path)) as conn:
        result = SigenergyIntegrationService(
            conn,
            client=client,
            now=lambda: NOW,
        ).verify_system_access("SYSTEM-1")
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "assets",
                "asset_integrations",
                "installation_imports",
                "integration_realtime_snapshots",
                "sigenergy_onboarding_requests",
            )
        }
        event = conn.execute(
            """
            SELECT operation, external_id, status, http_status
            FROM provider_operation_events
            WHERE operation = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (OPERATION_ACCESS,),
        ).fetchone()

    assert result.outcome is outcome
    assert result.access_status is access_status
    assert client.energy_calls == ["SYSTEM-1"]
    assert client.discovery_calls == 0
    assert counts == {
        "assets": 0,
        "asset_integrations": 0,
        "installation_imports": 0,
        "integration_realtime_snapshots": 0,
        "sigenergy_onboarding_requests": 0,
    }
    assert event["external_id"] == "SYSTEM-1"


def test_direct_access_success_persists_only_inventory_and_audit(
    tmp_path,
) -> None:
    db_path = _database(tmp_path, "access-success.db")
    client = AccessClient(
        flow={
            "pvPower": 2.5,
            "loadPower": 1.25,
            "systemStatus": "Normal",
        }
    )
    with get_db(str(db_path)) as conn:
        result = SigenergyIntegrationService(
            conn,
            client=client,
            now=lambda: NOW,
        ).verify_system_access("SYSTEM-1", external_name="Demo")
        inventory = conn.execute(
            """
            SELECT access_status, validation_method, operational_status,
                   first_access_at, last_access_at
            FROM provider_system_inventory
            WHERE external_id = 'SYSTEM-1'
            """
        ).fetchone()
        asset_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        mapping_count = conn.execute(
            "SELECT COUNT(*) FROM asset_integrations"
        ).fetchone()[0]
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM integration_realtime_snapshots"
        ).fetchone()[0]
        onboarding_count = conn.execute(
            "SELECT COUNT(*) FROM sigenergy_onboarding_requests"
        ).fetchone()[0]

    assert result.outcome is AccessOutcome.ACCESSIBLE
    assert result.access_status is AccessStatus.ACCESSIBLE
    assert inventory["access_status"] == "accessible"
    assert inventory["validation_method"] == "direct_energy_flow"
    assert inventory["operational_status"] == "operational"
    assert inventory["first_access_at"] == NOW
    assert inventory["last_access_at"] == NOW
    assert asset_count == 0
    assert mapping_count == 0
    assert snapshot_count == 0
    assert onboarding_count == 0


def test_invalid_direct_payload_is_provider_error(tmp_path) -> None:
    db_path = _database(tmp_path, "access-invalid-payload.db")
    with get_db(str(db_path)) as conn:
        result = SigenergyIntegrationService(
            conn,
            client=AccessClient(flow=["invalid"]),
            now=lambda: NOW,
        ).verify_system_access("SYSTEM-1")

    assert result.outcome is AccessOutcome.PROVIDER_ERROR


def test_invalid_system_id_is_rejected_before_any_remote_call(tmp_path) -> None:
    db_path = _database(tmp_path, "access-invalid-id.db")
    client = AccessClient()
    with get_db(str(db_path)) as conn:
        result = SigenergyIntegrationService(
            conn,
            client=client,
            now=lambda: NOW,
        ).verify_system_access("bad/id")
        event = conn.execute(
            """
            SELECT external_id, operation, status
            FROM provider_operation_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert result.outcome is AccessOutcome.PROVIDER_ERROR
    assert client.energy_calls == []
    assert event["external_id"] == "bad/id"
    assert event["operation"] == OPERATION_ACCESS


def test_mapping_is_idempotent_audited_and_never_selects_primary(
    tmp_path,
) -> None:
    db_path = _database(tmp_path, "mapping.db")
    with get_db(str(db_path)) as conn:
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Demo')"
            ).lastrowid
        )
        SigenergyIntegrationService(
            conn,
            client=AccessClient(),
            now=lambda: NOW,
        ).verify_system_access("SYSTEM-1", external_name="Demo")
        service = SigenergyMappingService(conn, now=lambda: NOW)
        first = service.map_system(
            external_id="SYSTEM-1",
            asset_id=asset_id,
            actor="tester",
        )
        second = service.map_system(
            external_id="SYSTEM-1",
            asset_id=asset_id,
            actor="tester",
        )
        mappings = conn.execute(
            """
            SELECT COUNT(*) AS total, MAX(is_primary_energy_source) AS primary_
            FROM asset_integrations
            WHERE provider = 'Sigenergy' AND external_id = 'SYSTEM-1'
            """
        ).fetchone()
        events = conn.execute(
            """
            SELECT COUNT(*)
            FROM provider_operation_events
            WHERE operation = ? AND external_id = 'SYSTEM-1'
            """,
            (OPERATION_MAPPING,),
        ).fetchone()[0]

    assert first.value == "associated"
    assert second.value == "associated"
    assert mappings["total"] == 1
    assert mappings["primary_"] == 0
    assert events == 2


def test_mapping_can_be_removed_after_access_is_lost(tmp_path) -> None:
    db_path = _database(tmp_path, "mapping-remove-after-access-loss.db")
    with get_db(str(db_path)) as conn:
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Demo')"
            ).lastrowid
        )
        SigenergyIntegrationService(
            conn,
            client=AccessClient(),
            now=lambda: NOW,
        ).verify_system_access("SYSTEM-1")
        service = SigenergyMappingService(conn, now=lambda: NOW)
        service.map_system(external_id="SYSTEM-1", asset_id=asset_id)
        conn.execute(
            """
            UPDATE provider_system_inventory
            SET access_status = 'unauthorized'
            WHERE provider = 'Sigenergy' AND external_id = 'SYSTEM-1'
            """
        )

        status = service.map_system(
            external_id="SYSTEM-1",
            asset_id=None,
            actor="tester",
        )
        mapping_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM asset_integrations
            WHERE provider = 'Sigenergy' AND external_id = 'SYSTEM-1'
            """
        ).fetchone()[0]

    assert status.value == "unassociated"
    assert mapping_count == 0


def test_sync_targets_mappings_directly_and_continues_after_failure(
    tmp_path,
) -> None:
    db_path = _database(tmp_path, "sync-isolation.db")
    client = AccessClient(
        flow={
            "pvPower": 2.5,
            "loadPower": 1.5,
            "systemStatus": "Normal",
        },
        errors={
            "SYSTEM-FAIL": SigenergyApiError(
                "provider failure",
                status_code=500,
            )
        },
    )
    with get_db(str(db_path)) as conn:
        failed_asset = _insert_asset_mapping(conn, "SYSTEM-FAIL")
        ok_asset = _insert_asset_mapping(conn, "SYSTEM-OK")
        _insert_asset_mapping(conn, "SYSTEM-DISABLED", enabled=0)
        batch = SigenergyIntegrationService(
            conn,
            client=client,
            now=lambda: NOW,
            today=lambda: date(2026, 7, 30),
        ).sync_all_mappings()
        snapshots = [
            dict(row)
            for row in conn.execute(
                """
                SELECT asset_id, external_id
                FROM integration_realtime_snapshots
                ORDER BY external_id
                """
            )
        ]
        states = {
            row["external_id"]: row["status"]
            for row in conn.execute(
                """
                SELECT external_id, status
                FROM provider_operation_state
                WHERE operation = ?
                """,
                (OPERATION_STATE_SYNC,),
            )
        }

    assert batch.status is SyncStatus.PARTIAL
    assert [result.external_id for result in batch.systems] == [
        "SYSTEM-FAIL",
        "SYSTEM-OK",
    ]
    assert client.energy_calls == ["SYSTEM-FAIL", "SYSTEM-OK"]
    assert client.discovery_calls == 0
    assert snapshots == [{"asset_id": ok_asset, "external_id": "SYSTEM-OK"}]
    assert failed_asset != ok_asset
    assert states == {"SYSTEM-FAIL": "failed", "SYSTEM-OK": "success"}


def test_sync_rate_limit_stops_new_calls_and_scopes_remaining_ids(
    tmp_path,
) -> None:
    db_path = _database(tmp_path, "sync-rate-limit.db")
    cooldown_until = datetime(2026, 7, 30, 19, 0, 0)
    client = AccessClient(
        errors={
            "SYSTEM-LIMIT": ApiRateLimitError(
                "Sigenergy",
                "state",
                cooldown_until,
                "rate limited",
            )
        }
    )
    with get_db(str(db_path)) as conn:
        _insert_asset_mapping(conn, "SYSTEM-LIMIT")
        _insert_asset_mapping(conn, "SYSTEM-NEXT")

        batch = SigenergyIntegrationService(
            conn,
            client=client,
            now=lambda: NOW,
            today=lambda: date(2026, 7, 30),
        ).sync_all_mappings()
        events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT external_id, status, http_status
                FROM provider_operation_events
                WHERE operation = ?
                ORDER BY id
                """,
                (OPERATION_STATE_SYNC,),
            )
        ]

    assert batch.status is SyncStatus.RATE_LIMITED
    assert client.energy_calls == ["SYSTEM-LIMIT"]
    assert [result.status for result in batch.systems] == [
        SyncStatus.RATE_LIMITED,
        SyncStatus.RATE_LIMITED,
    ]
    assert all(
        result.cooldown_until == cooldown_until for result in batch.systems
    )
    assert events == [
        {
            "external_id": "SYSTEM-LIMIT",
            "status": "rate_limited",
            "http_status": 429,
        },
        {
            "external_id": "SYSTEM-NEXT",
            "status": "rate_limited",
            "http_status": 429,
        },
    ]


def test_explicit_sync_target_never_filters_a_discovery_response(
    tmp_path,
) -> None:
    db_path = _database(tmp_path, "sync-explicit.db")
    client = AccessClient(
        flow={"pvPower": 1.0, "systemStatus": "Normal"}
    )
    with get_db(str(db_path)) as conn:
        asset_id = _insert_asset_mapping(conn, "EXPLICIT-ID")
        batch = SigenergyIntegrationService(
            conn,
            client=client,
            now=lambda: NOW,
            today=lambda: date(2026, 7, 30),
        ).sync_all_mappings(target_external_ids=["EXPLICIT-ID"])
        snapshot = conn.execute(
            """
            SELECT asset_id, external_id
            FROM integration_realtime_snapshots
            """
        ).fetchone()

    assert batch.status is SyncStatus.SUCCESS
    assert client.energy_calls == ["EXPLICIT-ID"]
    assert client.discovery_calls == 0
    assert dict(snapshot) == {
        "asset_id": asset_id,
        "external_id": "EXPLICIT-ID",
    }


def test_direct_access_never_changes_provider_onboarding_state(tmp_path) -> None:
    db_path = _database(tmp_path, "onboarding-independent.db")
    with get_db(str(db_path)) as conn:
        onboarding = SigenergyOnboardingService(
            conn,
            submit=lambda _system_id: {
                "status": "provider_pending",
                "provider_code": "1401",
                "message": "pending",
            },
            now=lambda: NOW,
        ).request_access("SYSTEM-1")
        SigenergyIntegrationService(
            conn,
            client=AccessClient(),
            now=lambda: "2026-07-30T19:00:00",
        ).verify_system_access("SYSTEM-1")
        stored = conn.execute(
            """
            SELECT status, approved_at
            FROM sigenergy_onboarding_requests
            WHERE id = ?
            """,
            (onboarding.request_id,),
        ).fetchone()

    assert stored["status"] == "provider_pending"
    assert stored["approved_at"] is None
