"""Huawei SCADA energy reaching a report, and saying what kind of number it is.

Two things have to hold at once. A report must be able to consume these facts
like any other -- that is the acceptance criterion. And it must not present an
integrated estimate as if it were a metered total, which is why the dataset and
the payload both carry the provenance.

The job wiring is here too, because it is the same claim from the other end:
rollup and retention are ordinary durable jobs that never touch a provider.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.jobs.handlers import execute
from nemsei.jobs.repository import ClaimedJob, JobRepository
from nemsei.jobs.scheduler import Scheduler
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.assembler import assemble_asset_report
from nemsei.reporting.datasets import build_dataset
from nemsei.reporting.periods import monthly_period
from nemsei.sync.models import ProviderRequestAttempt, SyncRun
from tests_v2.test_migrations import upgrade

MONTH = date(2026, 8, 1)


def integrated_metadata(**overrides):
    """What `rollup.py` actually stamps on a fact."""
    base = {
        "measurement_method": "power_integral",
        "estimated": True,
        "integration_rule": "trapezoidal",
        "sample_count": 288,
        "coverage_ratio": 0.996,
    }
    base.update(overrides)
    return base


@pytest.fixture
def reported(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Piloto SDongle", timezone="Europe/Lisbon")
        connection = create_connection(
            session,
            provider_code="huawei_scada",
            connection_key="scada-pilot",
            display_name="Huawei SCADA pilot",
            credential_reference="primary",
            enabled=True,
            configuration_status="configured",
        )
        session.flush()
        mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="HV2340123456"
        )
        session.flush()
        for day in (10, 11, 12):
            record_production_fact(
                session,
                asset_id=asset.id,
                provider_mapping_id=mapping.id,
                source_fact_key=f"huawei-scada:production_energy:2026-08-{day}",
                metric_kind="production_energy",
                period_start=datetime(2026, 8, day, tzinfo=timezone.utc),
                period_end=datetime(2026, 8, day + 1, tzinfo=timezone.utc),
                granularity="day",
                value=Decimal("100"),
                unit="kWh",
                quality="partial",
                completeness="complete",
                metadata=integrated_metadata(),
            )
        ids = {"asset": asset.id, "connection": connection.id, "mapping": mapping.id}
    return factory, ids


# --- the dataset --------------------------------------------------------------


def test_a_report_can_be_built_from_huawei_scada_facts(reported) -> None:
    """The acceptance criterion: reports consume these `ProductionFact` rows."""
    factory, ids = reported
    with factory() as session, session.begin():
        build_dataset(
            session, asset_id=ids["asset"], period_start=MONTH, period_end=date(2026, 9, 1), built_by="op"
        )
    with factory() as session:
        from nemsei.reporting.models import ReportingDatasetRow

        stored = session.scalar(select(ReportingDatasetRow))
        assert stored.actual_production_kwh == Decimal("300")
        # Integrated energy is never "measured", however complete the day was.
        assert stored.actual_state == "partial"


def test_the_dataset_names_the_facts_that_are_estimates(reported) -> None:
    factory, ids = reported
    with factory() as session, session.begin():
        dataset = build_dataset(
            session, asset_id=ids["asset"], period_start=MONTH, period_end=date(2026, 9, 1), built_by="op"
        )
        digest = dataset.input_digest
        quality = dict(dataset.quality_json)
        warnings = list(dataset.warnings_json)
    with factory() as session:
        from nemsei.reporting.models import ReportingDatasetRow

        stored = session.scalar(select(ReportingDatasetRow))
        estimated = stored.provenance_json["estimated_fact_keys"]
        assert len(estimated) == 3
        assert all(key.startswith("huawei-scada:") for key in estimated)
    assert quality["actual_sources"] == ["huawei-scada"]
    assert quality["months_with_estimated_energy"] == 1
    assert "estimated_energy:2026-08-01" in warnings
    assert digest


def test_a_dataset_with_no_estimated_fact_carries_no_estimation_key(settings, monkeypatch) -> None:
    """Adding the key unconditionally would change every existing digest.

    `input_digest` covers the row provenance, so a new key on every dataset
    would make every previously frozen snapshot look like new input without a
    single number having changed.
    """
    upgrade(settings, monkeypatch)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Metered plant")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        session.flush()
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1")
        session.flush()
        record_production_fact(
            session, asset_id=asset.id, provider_mapping_id=mapping.id,
            source_fact_key="fusionsolar:production_energy:2026-08-10",
            period_start=datetime(2026, 8, 10, tzinfo=timezone.utc),
            period_end=datetime(2026, 8, 11, tzinfo=timezone.utc), granularity="day",
            value=Decimal("50"), unit="kWh", quality="complete", completeness="complete", metadata={},
        )
        build_dataset(session, asset_id=asset.id, period_start=MONTH, period_end=date(2026, 9, 1), built_by="op")
    with factory() as session:
        from nemsei.reporting.models import ReportingDatasetRow

        stored = session.scalar(select(ReportingDatasetRow))
        assert "estimated_fact_keys" not in stored.provenance_json
        assert stored.actual_state == "measured"


# --- the payload --------------------------------------------------------------


def test_the_payload_says_where_the_energy_came_from_and_that_it_is_estimated(reported) -> None:
    factory, ids = reported
    with factory() as session, session.begin():
        report = assemble_asset_report(
            session, asset_id=ids["asset"], period=monthly_period("2026-08"), built_by="op"
        )

    assert report.payload["energy_estimated"] is True
    assert report.payload["energy_estimated_months"] == 1
    assert report.payload["energy_sources"] == ["huawei-scada"]
    assert "energy_integrated_from_power_samples_not_metered" in report.payload["report_notes"]
    assert any(note.startswith("estimated_energy:") for note in report.notes)
    assert report.payload["asset"]["energy_provider"] == "huawei_scada"


def test_assembling_a_report_makes_no_provider_request_at_all(reported) -> None:
    """Reports stay database-only: nothing here can reach a dongle or an API."""
    factory, ids = reported
    with factory() as session, session.begin():
        assemble_asset_report(session, asset_id=ids["asset"], period=monthly_period("2026-08"), built_by="op")

    with factory() as session:
        assert session.scalar(select(SyncRun)) is None
        assert session.scalar(select(ProviderRequestAttempt)) is None


def test_the_assembler_module_cannot_reach_the_listener_or_any_client() -> None:
    import ast
    from pathlib import Path

    source = Path("src/nemsei/reporting/assembler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not [name for name in imported if name.startswith("nemsei.integrations")]


# --- the jobs -----------------------------------------------------------------


def claimed(job_type: str, payload: dict) -> ClaimedJob:
    return ClaimedJob(id=1, job_type=job_type, payload=payload, attempt=1, lease_token="t", max_attempts=3)


def test_the_rollup_job_dispatches_to_the_rollup_service(reported, settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_POWER_UNIT", "kW")
    monkeypatch.setenv("NEMSEI_V2_HUAWEI_SCADA_PRIMARY_PRODUCTION_SIGNAL", "total_active_power")
    factory, ids = reported

    outcome = execute(
        claimed("huawei_scada.rollup", {"connection_id": ids["connection"], "lookback_days": 1}),
        testing=True,
        settings=settings,
        session_factory=factory,
    )

    assert outcome.status == "success"
    assert outcome.result["mode"] == "power_integral_rollup"
    # No samples exist in this fixture, so the day is skipped rather than zeroed.
    assert outcome.result["facts_written"] == 0


def test_the_retention_job_dispatches_to_the_retention_pass(reported, settings) -> None:
    factory, ids = reported

    outcome = execute(
        claimed("huawei_scada.retention", {"connection_id": ids["connection"], "retention_days": 90}),
        testing=True,
        settings=settings,
        session_factory=factory,
    )

    assert outcome.status == "success"
    assert outcome.result["mode"] == "retention"
    assert outcome.result["samples_deleted"] == 0


def test_a_huawei_job_without_a_connection_id_is_refused_loudly(reported, settings) -> None:
    factory, _ids = reported
    with pytest.raises(ValueError, match="connection_id"):
        execute(claimed("huawei_scada.rollup", {}), testing=True, settings=settings, session_factory=factory)


def test_the_scheduler_enqueues_the_rollup_only_when_it_is_configured(settings, monkeypatch) -> None:
    from dataclasses import replace

    upgrade(settings, monkeypatch)
    # One owner token for both instances: the scheduler lease is a single
    # shared row, so a second token would simply be starved rather than
    # testing anything about the schedules.
    off = Scheduler(settings, owner_token="test-scheduler")
    assert off.repository.enqueue_due_huawei_scada_rollup is not None
    # Off by default: one tick creates the hourly noop and nothing Huawei.
    off.run_once()
    with build_session_factory(create_engine(settings.database_url))() as session:
        from nemsei.jobs.models import Job

        assert not [job for job in session.scalars(select(Job)) if job.job_type.startswith("huawei_scada")]

    on = Scheduler(
        replace(
            settings,
            huawei_scada_rollup_enabled=True,
            huawei_scada_rollup_connection_id=3,
            huawei_scada_retention_enabled=True,
        ),
        owner_token="test-scheduler",
    )
    on.run_once()
    with build_session_factory(create_engine(settings.database_url))() as session:
        from nemsei.jobs.models import Job

        types = {job.job_type for job in session.scalars(select(Job))}
        assert {"huawei_scada.rollup", "huawei_scada.retention"} <= types


def test_a_second_scheduler_tick_does_not_enqueue_the_same_cycle_twice(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    repository = JobRepository(
        create_engine(settings.database_url), build_session_factory(create_engine(settings.database_url))
    )

    first, created_first = repository.enqueue_due_huawei_scada_rollup(
        connection_id=3, interval_minutes=60, lookback_days=2
    )
    second, created_second = repository.enqueue_due_huawei_scada_rollup(
        connection_id=3, interval_minutes=60, lookback_days=2
    )

    assert created_first and first is not None
    assert not created_second and second is None
