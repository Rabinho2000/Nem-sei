"""Sync runs whose owner died, and the only honest thing to say about them."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.huawei_scada.abandonment import scada_session_liveness
from nemsei.integrations.huawei_scada.models import HuaweiScadaSession
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sync.abandonment import (
    ABANDONED_ERROR_CODE,
    sweep_abandoned_sync_runs,
)
from nemsei.sync.models import IntegrationHealth, SyncRun
from nemsei.sync.service import finish_sync_run, record_request_result, reserve_request, start_sync_run
from tests_v2.test_migrations import upgrade


NOW = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def connection_for(factory, provider_code="fusionsolar"):
    with factory() as session:
        connection = create_connection(
            session,
            provider_code=provider_code,
            connection_key=f"{provider_code}-abandonment",
            display_name="Abandonment fixture",
            credential_reference="primary",
            enabled=True,
            configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Abandonment asset")
        create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id="FS-001",
            valid_from=date(2020, 1, 1),
        )
        session.commit()
        return connection.id


def open_run(factory, connection_id, *, started_at, capability="production_history"):
    with factory() as session:
        run = start_sync_run(session, provider_connection_id=connection_id, capability=capability)
        session.flush()
        run.started_at = started_at
        run_id = run.id
        session.commit()
    return run_id


def test_a_run_abandoned_days_ago_is_classified_not_left_running(settings, monkeypatch) -> None:
    """Run 4, as found in production: opened 2026-08-19, one day fetched, silence.

    Nothing can finish it -- `finish_sync_run` only accepts a run from the
    owner that is still holding it, and that process has been gone for nine
    days. Until this sweep existed, the row simply said `running` forever.
    """
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory)
    run_id = open_run(factory, connection_id, started_at=NOW - timedelta(days=12))

    with factory() as session:
        state, attempt, _allowed = reserve_request(
            session,
            provider_connection_id=connection_id,
            endpoint_family="production_history_daily",
            purpose="fusionsolar_daily_production_2026-07-24",
            sync_run_id=run_id,
            now=NOW - timedelta(days=12),
        )
        record_request_result(session, state=state, attempt=attempt, now=NOW - timedelta(days=12))
        session.commit()

    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))

    assert sweep.examined == 1 and sweep.abandoned_count == 1
    with factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == "failed"
        assert run.error_code == ABANDONED_ERROR_CODE
        # Closed at the last moment it is known to have been alive, not at the
        # moment it was noticed: it did not run for twelve days.
        assert run.finished_at == NOW - timedelta(days=12)
        assert run.metadata_json["abandoned"] is True
        assert run.metadata_json["abandoned_detected_at"] == NOW.isoformat()


def test_a_run_that_is_merely_slow_is_left_alone(settings, monkeypatch) -> None:
    """Recent evidence of work is evidence of an owner, and the sweep waits."""
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory)
    run_id = open_run(factory, connection_id, started_at=NOW - timedelta(days=3))

    with factory() as session:
        state, attempt, _allowed = reserve_request(
            session,
            provider_connection_id=connection_id,
            endpoint_family="production_history_daily",
            purpose="fusionsolar_daily_production_2026-08-30",
            sync_run_id=run_id,
            now=NOW - timedelta(minutes=2),
        )
        record_request_result(session, state=state, attempt=attempt, now=NOW - timedelta(minutes=2))
        session.commit()

    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))
    assert sweep.abandoned_count == 0
    with factory() as session:
        assert session.get(SyncRun, run_id).status == "running"


def test_a_finished_run_is_never_reopened_or_rewritten(settings, monkeypatch) -> None:
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory)
    with factory() as session:
        run = start_sync_run(session, provider_connection_id=connection_id, capability="production_history")
        session.flush()
        run.started_at = NOW - timedelta(days=9)
        finish_sync_run(session, run=run, status="partial", completeness="partial")
        run_id = run.id
        session.commit()

    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))
    assert sweep.examined == 0 and sweep.abandoned_count == 0
    with factory() as session:
        assert session.get(SyncRun, run_id).status == "partial"


def test_a_long_lived_scada_session_that_is_still_reporting_is_not_swept(settings, monkeypatch) -> None:
    """The case a plain timeout would get wrong.

    A dongle session makes no outbound call, so it leaves no request attempt
    behind and looks silent to the provider-neutral rule. Its heartbeat is its
    own session row, which the adapter's resolver reads.
    """
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory, provider_code="huawei_scada")
    run_id = open_run(factory, connection_id, started_at=NOW - timedelta(hours=6), capability="current_monitoring")
    with factory() as session:
        session.add(
            HuaweiScadaSession(
                dongle_serial="TA2330010373",
                provider_connection_id=connection_id,
                session_state="polling",
                opened_at=NOW - timedelta(hours=6),
                last_seen_at=NOW - timedelta(minutes=1),
                metadata_json={"sync_run_id": run_id},
            )
        )
        session.commit()

    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))
    assert sweep.abandoned_count == 0
    with factory() as session:
        assert session.get(SyncRun, run_id).status == "running"


def test_a_closed_scada_session_abandons_its_run_without_waiting(settings, monkeypatch) -> None:
    """The strong proof: the owner reached its own end and did not close the run."""
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory, provider_code="huawei_scada")
    run_id = open_run(factory, connection_id, started_at=NOW - timedelta(minutes=20), capability="current_monitoring")
    with factory() as session:
        session.add(
            HuaweiScadaSession(
                dongle_serial="TA2330010373",
                provider_connection_id=connection_id,
                session_state="closed",
                close_reason="peer_closed",
                opened_at=NOW - timedelta(minutes=20),
                last_seen_at=NOW - timedelta(minutes=2),
                closed_at=NOW - timedelta(minutes=2),
                metadata_json={"sync_run_id": run_id},
            )
        )
        session.commit()

    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))
    assert sweep.abandoned_count == 1
    with factory() as session:
        run = session.get(SyncRun, run_id)
        assert run.status == "failed" and run.error_code == ABANDONED_ERROR_CODE
        assert "closed without finishing it" in run.safe_detail


def test_sweeping_does_not_rewrite_how_the_connection_is_doing_now(settings, monkeypatch) -> None:
    """A run that went quiet nine days ago is not news about today.

    `finish_sync_run` would move `last_failure_at` to now and mark the
    connection degraded, which would make a healthy connection look broken
    because of something that stopped happening last week.
    """
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory)
    open_run(factory, connection_id, started_at=NOW - timedelta(days=9))
    with factory() as session:
        healthy = start_sync_run(session, provider_connection_id=connection_id, capability="current_monitoring")
        session.flush()
        finish_sync_run(session, run=healthy, status="success", completeness="complete")
        session.commit()
    with factory() as session:
        before = session.scalar(select(IntegrationHealth).where(IntegrationHealth.provider_connection_id == connection_id))
        was = (before.sync_state, before.last_failure_at, before.last_error_code)

    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))
    assert sweep.abandoned_count == 1
    with factory() as session:
        after = session.scalar(select(IntegrationHealth).where(IntegrationHealth.provider_connection_id == connection_id))
        assert (after.sync_state, after.last_failure_at, after.last_error_code) == was
        assert after.sync_state == "healthy"


def test_the_sweep_reports_a_provider_neutral_summary(settings, monkeypatch) -> None:
    """Nothing in the job result names a credential, a station or an endpoint."""
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory)
    open_run(factory, connection_id, started_at=NOW - timedelta(days=4))
    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))
    assert sweep.abandoned_count == 1
    only = sweep.abandoned[0]
    assert only.capability == "production_history"
    assert only.reason == "the run made no provider call at all after opening"


def test_an_error_carrying_run_still_running_is_swept_like_any_other(settings, monkeypatch) -> None:
    """The status is what makes a run sweepable, not what went wrong in it."""
    factory = factory_for(settings, monkeypatch)
    connection_id = connection_for(factory)
    run_id = open_run(factory, connection_id, started_at=NOW - timedelta(days=2))
    with factory() as session:
        state, attempt, _allowed = reserve_request(
            session,
            provider_connection_id=connection_id,
            endpoint_family="production_history_daily",
            purpose="fusionsolar_daily_production_2026-08-29",
            sync_run_id=run_id,
            now=NOW - timedelta(days=2),
        )
        record_request_result(
            session,
            state=state,
            attempt=attempt,
            error=ProviderError(ProviderErrorCode.RATE_LIMITED, "later", transient=True),
            now=NOW - timedelta(days=2),
        )
        session.commit()
    sweep = sweep_abandoned_sync_runs(factory, now=NOW, owner_resolvers=(scada_session_liveness,))
    assert sweep.abandoned_count == 1
