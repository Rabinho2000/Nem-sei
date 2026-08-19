from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, HttpResponse
from nemsei.integrations.fusionsolar.production import FusionSolarProductionService, normalize_daily_production_row
from nemsei.monitoring.models import ProductionFact
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import ProviderRequestAttempt, ProviderRequestState, SyncCursor, SyncRun
from tests_v2.test_migrations import upgrade


LOGIN_OK = {"success": True, "failCode": 0, "data": None}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, payload, headers, timeout_seconds):
        self.calls.append((url, payload, headers))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        # The real endpoint answers with one row per day of the month, each
        # carrying its own collectTime. These fixtures describe the requested
        # day, so stamp it; multi-day attribution has its own test file.
        if "collectTime" in payload and isinstance(getattr(value, "payload", None), dict):
            for entry in value.payload.get("data") or []:
                if isinstance(entry, dict) and "collectTime" not in entry:
                    entry["collectTime"] = payload["collectTime"]
        return value


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, payload)


def daily(rows):
    return response({"success": True, "failCode": 0, "data": rows})


def row(code, value):
    values = {} if value is None else {"PVYield": value}
    return {"stationCode": code, "dataItemMap": values}


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def configured_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_BASE_URL", "https://fusion.example.test")
    # The test contract is deliberately explicit; no Lisbon/UTC default exists.
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_TIMEZONE", "UTC")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_UNIT", "kWh")


def selected_connection(factory, *, count=1):
    with factory() as session:
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="fusion-production",
            display_name="Fusion production",
            credential_reference="production",
            enabled=True,
            configuration_status="configured",
        )
        mappings = []
        for number in range(1, count + 1):
            asset = create_asset(session, canonical_name=f"Production asset {number}")
            mapping = create_mapping(
                session,
                asset_id=asset.id,
                provider_connection_id=connection.id,
                external_id=f"FS-{number:03d}",
                valid_from=date(2020, 1, 1),
            )
            create_source_policy(
                session,
                asset_id=asset.id,
                provider_mapping_id=mapping.id,
                source_use="production",
                priority=1,
                valid_from=date(2020, 1, 1),
            )
            mappings.append(mapping)
        session.commit()
        return connection.id, mappings


def service(factory, settings, transport, *, retries=0):
    configured = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    return FusionSolarProductionService(
        factory,
        configured,
        client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport),
        max_transient_retries=retries,
    )


def test_daily_production_persists_provider_neutral_fact_and_advances_cursor(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    transport = FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "token"}), daily([row("FS-001", "123.45")])])
    result = service(factory, settings, transport).sync_daily_production(connection_id, start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))
    assert result.status == "success" and result.cursor_advanced and result.accepted == 1
    assert [call[0].rsplit("/", 1)[-1] for call in transport.calls] == ["login", "getKpiStationDay"]
    assert transport.calls[1][1] == {"stationCodes": "FS-001", "collectTime": 1768435200000}
    with factory() as session:
        fact = session.scalar(select(ProductionFact))
        cursor = session.scalar(select(SyncCursor))
        run = session.get(SyncRun, result.sync_run_id)
        assert fact is not None and fact.provider_mapping_id == mappings[0].id
        assert fact.value == Decimal("123.45") and fact.unit == "kWh" and fact.granularity == "day"
        assert fact.period_start == datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert fact.period_end == datetime(2026, 1, 16, tzinfo=timezone.utc)
        assert fact.metadata_json == {"source_period_timezone": "UTC", "source_period_date": "2026-01-15", "provider_value_field": "PVYield"}
        assert cursor is not None and cursor.checkpoint_json["last_completed_day"] == "2026-01-15"
        assert run.metadata_json["actual_provider_calls"] == 2


def test_zero_and_missing_are_distinct_and_missing_day_does_not_advance_cursor(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory, count=2)
    result = service(factory, settings, FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "0"), row("FS-002", None)])])).sync_daily_production(
        connection_id, start_date=date(2026, 1, 15), end_date=date(2026, 1, 15)
    )
    assert result.status == "partial" and not result.cursor_advanced
    with factory() as session:
        facts = list(session.scalars(select(ProductionFact).order_by(ProductionFact.provider_mapping_id)))
        assert [(item.provider_mapping_id, item.value, item.quality, item.completeness) for item in facts] == [
            (mappings[0].id, Decimal("0"), "complete", "complete"),
            (mappings[1].id, None, "missing", "partial"),
        ]
        assert session.scalar(select(SyncCursor)) is None


def test_multi_day_idempotency_correction_and_restart_from_safe_cursor(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    first = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row("FS-001", "120.4")]),
        daily([row("FS-001", "121.0")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 1, 14), end_date=date(2026, 1, 15))
    assert first.status == "success"
    restart = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row("FS-001", "121.0")]),
    ])).sync_daily_production(connection_id, end_date=date(2026, 1, 16), reconciliation_days=0)
    assert restart.status == "success" and restart.requested_from == date(2026, 1, 16)
    corrected = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "121.1")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))
    assert corrected.status == "success"
    with factory() as session:
        facts = list(session.scalars(select(ProductionFact).where(ProductionFact.provider_mapping_id == mappings[0].id).order_by(ProductionFact.source_fact_key, ProductionFact.source_revision)))
        assert [(fact.source_fact_key[-10:], fact.source_revision, fact.value) for fact in facts] == [
            ("2026-01-14", 1, Decimal("120.4")),
            ("2026-01-15", 1, Decimal("121.0")),
            ("2026-01-15", 2, Decimal("121.1")),
            ("2026-01-16", 1, Decimal("121.0")),
        ]
        assert facts[2].supersedes_fact_id == facts[1].id


def test_partial_missing_row_and_provider_failures_do_not_skip_cursor_coverage(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    partial = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row("FS-001", "10")]),
        daily([]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 1, 14), end_date=date(2026, 1, 15))
    assert partial.status == "partial" and not partial.cursor_advanced
    failed = service(factory, settings, FakeTransport([
        FusionSolarClientError(ProviderError(ProviderErrorCode.TIMEOUT, "timeout", transient=True)),
    ])).sync_daily_production(connection_id, start_date=date(2026, 1, 14), end_date=date(2026, 1, 14))
    assert failed.status == "failed" and not failed.cursor_advanced
    with factory() as session:
        assert session.scalar(select(SyncCursor)) is None
        assert session.scalar(select(func.count()).select_from(ProductionFact)) == 1


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "bad auth")), "failed"),
        (FusionSolarClientError(ProviderError(ProviderErrorCode.RATE_LIMITED, "later", retry_after_seconds=60, transient=True)), "rate_limited"),
    ],
)
def test_authentication_and_rate_limit_are_controlled_without_facts(settings, monkeypatch, error, status):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    result = service(factory, settings, FakeTransport([error])).sync_daily_production(connection_id, start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))
    assert result.status == status
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProductionFact)) == 0
        if status == "rate_limited":
            state = session.scalar(select(ProviderRequestState).where(ProviderRequestState.endpoint_family == "authentication"))
            assert state is not None and state.provider_retry_at is not None


def test_source_policy_conflict_blocks_call_and_untrusted_contract_blocks_persistence(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, mappings = selected_connection(factory)
    with factory() as session:
        duplicate = create_mapping(
            session,
            asset_id=mappings[0].asset_id,
            provider_connection_id=connection_id,
            external_id="FS-002",
            valid_from=date(2020, 1, 1),
        )
        create_source_policy(session, asset_id=mappings[0].asset_id, provider_mapping_id=duplicate.id, source_use="production", priority=1, valid_from=date(2020, 1, 1))
        session.commit()
    transport = FakeTransport([])
    result = service(factory, settings, transport).sync_daily_production(connection_id, start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))
    assert result.status == "failed" and not transport.calls
    monkeypatch.delenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_UNIT")
    contract_failure = service(factory, settings, FakeTransport([])).sync_daily_production(connection_id, start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))
    assert contract_failure.status == "failed" and contract_failure.error_code == "configuration"
    with factory() as session:
        assert session.get(SyncRun, contract_failure.sync_run_id) is not None
        assert session.scalar(select(func.count()).select_from(ProductionFact)) == 0


def test_normalizer_rejects_unverified_fallback_fields_and_no_discovery_occurs(settings, monkeypatch):
    assert normalize_daily_production_row({"stationCode": "FS-001", "dataItemMap": {"inverterYield": "10"}}).value is None
    with pytest.raises(ValueError, match="not numeric"):
        normalize_daily_production_row(row("FS-001", "not-a-number"))
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    transport = FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "1")])])
    result = service(factory, settings, transport).sync_daily_production(connection_id, start_date=date(2026, 1, 15), end_date=date(2026, 1, 15))
    assert result.status == "success"
    assert all("/stations" not in call[0] for call in transport.calls)
    with factory() as session:
        attempts = list(session.scalars(select(ProviderRequestAttempt).where(ProviderRequestAttempt.sync_run_id == result.sync_run_id)))
        assert [attempt.status for attempt in attempts] == ["succeeded", "succeeded"]


def test_cursor_advances_only_for_contiguous_selected_coverage(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    baseline = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "10")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10))
    assert baseline.cursor_advanced
    gap = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row("FS-001", "15")]), daily([row("FS-001", "16")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 15), end_date=date(2026, 8, 16))
    assert gap.status == "success" and not gap.cursor_advanced
    next_day = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "11")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 11), end_date=date(2026, 8, 11))
    assert next_day.cursor_advanced
    overlap = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}),
        daily([row("FS-001", "11")]), daily([row("FS-001", "12")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 11), end_date=date(2026, 8, 12))
    assert overlap.cursor_advanced
    with factory() as session:
        cursor = session.scalar(select(SyncCursor))
        assert cursor is not None and cursor.checkpoint_json["last_completed_day"] == "2026-08-12"


def test_historical_correction_partial_and_failure_never_move_existing_cursor(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "10")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10))
    correction = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "9.5")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 9), end_date=date(2026, 8, 9))
    partial = service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 11), end_date=date(2026, 8, 11))
    failed = service(factory, settings, FakeTransport([
        FusionSolarClientError(ProviderError(ProviderErrorCode.TIMEOUT, "timeout", transient=True)),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 11), end_date=date(2026, 8, 11))
    assert correction.status == "success" and not correction.cursor_advanced
    assert partial.status == "partial" and not partial.cursor_advanced
    assert failed.status == "failed" and not failed.cursor_advanced
    with factory() as session:
        cursor = session.scalar(select(SyncCursor))
        assert cursor is not None and cursor.checkpoint_json["last_completed_day"] == "2026-08-10"


@pytest.mark.parametrize("name, value", [
    ("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_TIMEZONE", None),
    ("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_UNIT", "MWh"),
])
def test_contract_configuration_and_disabled_reads_are_controlled_without_http(settings, monkeypatch, name, value):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    transport = FakeTransport([])
    result = service(factory, settings, transport).sync_daily_production(connection_id, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10))
    assert result.status == "failed" and result.error_code == "configuration" and not transport.calls
    disabled_transport = FakeTransport([])
    disabled = FusionSolarProductionService(
        factory,
        settings,
        client_factory=lambda credentials: FusionSolarClient(credentials, transport=disabled_transport),
    ).sync_daily_production(connection_id, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10))
    assert disabled.status == "deferred" and disabled.error_code == "not_supported" and not disabled_transport.calls
    with factory() as session:
        runs = list(session.scalars(select(SyncRun).order_by(SyncRun.id)))
        assert [run.status for run in runs] == ["failed", "deferred"]
        assert session.scalar(select(func.count()).select_from(ProductionFact)) == 0


def test_cursor_timezone_change_and_normal_window_limit_fail_safely(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, _mappings = selected_connection(factory)
    service(factory, settings, FakeTransport([
        response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), daily([row("FS-001", "10")]),
    ])).sync_daily_production(connection_id, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10))
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_TIMEZONE", "Europe/Lisbon")
    timezone_changed = service(factory, settings, FakeTransport([])).sync_daily_production(connection_id, end_date=date(2026, 8, 11), reconciliation_days=0)
    assert timezone_changed.status == "failed" and timezone_changed.error_code == "configuration"
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_PRODUCTION_PRODUCTION_TIMEZONE", "UTC")
    bounded = FusionSolarProductionService(
        factory,
        replace(settings, capabilities={**settings.capabilities, "provider_reads": True}, production_max_source_days=1),
        client_factory=lambda credentials: FusionSolarClient(credentials, transport=FakeTransport([])),
    ).sync_daily_production(connection_id, start_date=date(2026, 8, 11), end_date=date(2026, 8, 12))
    assert bounded.status == "failed" and bounded.error_code == "configuration"
    with factory() as session:
        cursor = session.scalar(select(SyncCursor))
        assert cursor is not None and cursor.checkpoint_json == {"last_completed_day": "2026-08-10", "source_timezone": "UTC"}
