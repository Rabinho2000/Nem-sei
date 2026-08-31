"""From a dialled-in dongle to persisted, attributable evidence.

These run against a real PostgreSQL, because everything worth proving here is
a database property: the revision rule that makes reconnects idempotent, the
quarantine that keeps an unknown serial away from someone else's asset, and
the source policy that decides whose canonical current state a sample becomes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.huawei_scada import protocol as p
from nemsei.integrations.huawei_scada.ingestion import (
    HuaweiScadaIngestion,
    quantise,
    sample_key,
    stale_open_sessions,
)
from nemsei.integrations.huawei_scada.models import (
    HuaweiScadaPendingDongle,
    HuaweiScadaPowerSample,
    HuaweiScadaSession,
)
from nemsei.integrations.huawei_scada.session import DownstreamProbe, PollOutcome
from nemsei.monitoring.models import MonitoringCurrentState, MonitoringObservation
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import IntegrationHealth, SyncRun
from tests_v2.test_huawei_scada_protocol import REAL_ADVERTISEMENT, aggregate_block
from tests_v2.test_migrations import upgrade

SERIAL = "HV2340123456"
BUCKET = 30


def advertisement(serial: str = SERIAL):
    banner = REAL_ADVERTISEMENT.replace(SERIAL.encode(), serial.encode()) + b")"
    parsed, _tail = p.extract_advertisement(banner)
    assert parsed is not None
    return parsed


def reading(**kwargs) -> p.AggregatedReading:
    return p.parse_aggregated_block(aggregate_block(**kwargs))


def outcome_at(moment: datetime, **kwargs) -> PollOutcome:
    return PollOutcome(observed_at=moment, reading=reading(**kwargs))


@pytest.fixture
def world(settings, monkeypatch):
    """One asset, one huawei_scada connection, one approved dongle mapping."""
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    with factory() as session, session.begin():
        connection = create_connection(
            session,
            provider_code="huawei_scada",
            connection_key="scada-pilot",
            display_name="Huawei SCADA pilot",
            credential_reference="primary",
            enabled=True,
            configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Piloto SDongle", timezone="Europe/Lisbon")
        session.flush()
        mapping = create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id=SERIAL,
            valid_from=datetime(2026, 1, 1).date(),
        )
        session.flush()
        ids = {"connection": connection.id, "asset": asset.id, "mapping": mapping.id}
    return factory, ids


def with_monitoring_policy(factory, ids, *, is_fallback: bool = False, priority: int = 1):
    with factory() as session, session.begin():
        create_source_policy(
            session,
            asset_id=ids["asset"],
            provider_mapping_id=ids["mapping"],
            source_use="monitoring",
            priority=priority,
            is_fallback=is_fallback,
            valid_from=datetime(2026, 1, 1).date(),
        )


def ingestion_for(factory, ids) -> HuaweiScadaIngestion:
    return HuaweiScadaIngestion(factory, connection_id=ids["connection"], sample_bucket_seconds=BUCKET)


def bind(ingestion, *, serial: str = SERIAL, peer: str = "fingerprint"):
    session_id = ingestion.open_session(peer_fingerprint=peer)
    binding = ingestion.identify(
        session_id=session_id,
        advertisement=advertisement(serial),
        describe={"dongle_model": "SDongle-WLAN-FE", "aggregate_unit_id": 100, "advertisement_fields": {}},
    )
    return session_id, binding


# --- identity ----------------------------------------------------------------


def test_an_approved_serial_binds_to_its_asset(world) -> None:
    factory, ids = world
    _session_id, binding = bind(ingestion_for(factory, ids))
    assert binding.is_bound
    assert binding.asset_id == ids["asset"]
    assert binding.mapping_id == ids["mapping"]


def test_an_unknown_serial_is_quarantined_and_never_bound(world) -> None:
    factory, ids = world
    session_id, binding = bind(ingestion_for(factory, ids), serial="HV9999999999")

    assert not binding.is_bound
    assert binding.reason == "unmapped_dongle"
    with factory() as session:
        pending = session.scalar(select(HuaweiScadaPendingDongle))
        assert pending.dongle_serial == "HV9999999999"
        assert pending.status == "pending"
        assert pending.advertisement_json["fields"]["4"] == "HV9999999999"
        row = session.get(HuaweiScadaSession, session_id)
        assert row.session_state == "quarantined"
        assert row.provider_mapping_id is None and row.asset_id is None


def test_an_unknown_dongle_that_keeps_knocking_is_counted_not_duplicated(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    for _ in range(3):
        bind(ingestion, serial="HV9999999999")
    with factory() as session:
        rows = list(session.scalars(select(HuaweiScadaPendingDongle)))
        assert len(rows) == 1
        assert rows[0].session_count == 3


def test_a_serial_an_operator_rejected_stays_rejected_however_often_it_calls(world) -> None:
    """A rejection is a decision, not a rate limit."""
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    bind(ingestion, serial="HV9999999999")
    with factory() as session, session.begin():
        session.scalar(select(HuaweiScadaPendingDongle)).status = "rejected"
    bind(ingestion, serial="HV9999999999")
    with factory() as session:
        pending = session.scalar(select(HuaweiScadaPendingDongle))
        assert pending.status == "rejected"
        assert pending.session_count == 1


def test_binding_never_consults_the_peer_address(world) -> None:
    """The rule the whole quarantine exists to protect.

    Two dongles arriving from the same public address -- one mapped, one not --
    must not become the same plant. The only input to the decision is the
    announced serial.
    """
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    _known_id, known = bind(ingestion, peer="same-router")
    _unknown_id, unknown = bind(ingestion, serial="HV0000000001", peer="same-router")
    assert known.is_bound and not unknown.is_bound


def test_the_peer_address_is_stored_only_as_a_salted_fingerprint(world) -> None:
    factory, ids = world
    from nemsei.integrations.huawei_scada.service import peer_fingerprint

    fingerprint = peer_fingerprint("203.0.113.9", salt="deployment-secret")
    ingestion = ingestion_for(factory, ids)
    bind(ingestion, peer=fingerprint)
    with factory() as session:
        row = session.scalar(select(HuaweiScadaSession))
        assert row.peer_fingerprint == fingerprint
        assert "203.0.113" not in (row.peer_fingerprint or "")
    # Same address, same digest; a different address, a different one.
    assert peer_fingerprint("203.0.113.9", salt="deployment-secret") == fingerprint
    assert peer_fingerprint("203.0.113.10", salt="deployment-secret") != fingerprint


def test_a_serial_that_becomes_mapped_stops_being_pending(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    with factory() as session, session.begin():
        session.add(
            HuaweiScadaPendingDongle(
                dongle_serial=SERIAL,
                status="pending",
                first_seen_at=datetime(2026, 8, 1, tzinfo=datetime_timezone.utc),
                last_seen_at=datetime(2026, 8, 1, tzinfo=datetime_timezone.utc),
                session_count=1,
                advertisement_json={},
                updated_at=datetime(2026, 8, 1, tzinfo=datetime_timezone.utc),
            )
        )
    bind(ingestion)
    with factory() as session:
        assert session.scalar(select(HuaweiScadaPendingDongle)).status == "mapped"


# --- samples -----------------------------------------------------------------


def test_a_sample_stores_kilowatts_and_the_registers_behind_them(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)
    moment = datetime(2026, 8, 25, 10, 0, 5, tzinfo=datetime_timezone.utc)

    result = ingestion.record_sample(session_id=session_id, binding=binding, outcome=outcome_at(moment))

    assert result.created and result.revision == 1
    with factory() as session:
        sample = session.get(HuaweiScadaPowerSample, result.sample_id)
        assert sample.total_active_power_kw == Decimal("5.400000")
        assert sample.grid_power_kw == Decimal("-2.312000")
        assert sample.raw_registers_json[str(p.REGISTER_PV_INPUT_POWER)] == 5432
        assert sample.quality == "complete"
        assert sample.metadata_json["unit"] == "kW"
        assert sample.metadata_json["observed_at_source"] == "listener_receive_time"
        # Quantised onto the sampling grid, not stored at the raw instant.
        assert sample.observed_at == quantise(moment, bucket_seconds=BUCKET)


def test_a_partial_reading_is_stored_as_partial_never_as_zero(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)
    moment = datetime(2026, 8, 25, 10, 0, tzinfo=datetime_timezone.utc)

    result = ingestion.record_sample(
        session_id=session_id, binding=binding, outcome=outcome_at(moment, pv=None, grid=None)
    )

    with factory() as session:
        sample = session.get(HuaweiScadaPowerSample, result.sample_id)
        assert sample.pv_input_power_kw is None
        assert sample.quality == "partial"
        assert sample.raw_registers_json[str(p.REGISTER_PV_INPUT_POWER)] is None


def test_re_reading_the_same_instant_after_a_reconnect_does_not_duplicate(world) -> None:
    """The acceptance criterion: reconnects must not duplicate data."""
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    moment = datetime(2026, 8, 25, 10, 0, 3, tzinfo=datetime_timezone.utc)

    first_session, binding = bind(ingestion)
    ingestion.record_sample(session_id=first_session, binding=binding, outcome=outcome_at(moment))
    ingestion.close_session(session_id=first_session, reason="peer_closed")

    # The dongle reconnects and re-reads the same interval with the same values.
    second_session, binding = bind(ingestion)
    repeat = ingestion.record_sample(
        session_id=second_session,
        binding=binding,
        outcome=outcome_at(moment + timedelta(seconds=4)),
    )

    assert not repeat.created
    with factory() as session:
        assert session.scalar(select(HuaweiScadaPowerSample.id).where(HuaweiScadaPowerSample.id != repeat.sample_id)) is None


def test_a_corrected_value_for_the_same_instant_supersedes_rather_than_duplicates(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    moment = datetime(2026, 8, 25, 10, 0, tzinfo=datetime_timezone.utc)
    session_id, binding = bind(ingestion)

    first = ingestion.record_sample(session_id=session_id, binding=binding, outcome=outcome_at(moment))
    second = ingestion.record_sample(
        session_id=session_id, binding=binding, outcome=outcome_at(moment + timedelta(seconds=10), pv=6000)
    )

    assert second.created and second.revision == 2
    with factory() as session:
        rows = list(session.scalars(select(HuaweiScadaPowerSample).order_by(HuaweiScadaPowerSample.source_revision)))
        assert [row.source_revision for row in rows] == [1, 2]
        assert rows[1].supersedes_sample_id == first.sample_id
        # Both rows share one identity; the newest revision is the reading.
        assert {row.source_sample_key for row in rows} == {sample_key(SERIAL, quantise(moment, bucket_seconds=BUCKET))}


def test_a_failed_poll_writes_no_sample_at_all(world) -> None:
    """A read that failed is not a measurement of zero."""
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, _binding = bind(ingestion)

    ingestion.record_poll_failure(
        session_id=session_id,
        outcome=PollOutcome(
            observed_at=datetime(2026, 8, 25, 10, 0, tzinfo=datetime_timezone.utc),
            error_code="timeout",
            safe_detail="No answer within the read timeout.",
        ),
    )

    with factory() as session:
        assert session.scalar(select(HuaweiScadaPowerSample)) is None
        row = session.scalar(select(HuaweiScadaSession))
        assert row.session_state == "degraded"
        assert row.error_count == 1
        assert row.metadata_json["poll_failures"] == {"timeout": 1}


# --- canonical monitoring -----------------------------------------------------


def test_a_generating_plant_confirms_an_operational_current_state(world) -> None:
    factory, ids = world
    with_monitoring_policy(factory, ids)
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)
    assert binding.monitoring_selected

    result = ingestion.record_sample(
        session_id=session_id,
        binding=binding,
        outcome=outcome_at(datetime(2026, 8, 25, 12, 0, tzinfo=datetime_timezone.utc)),
    )

    assert result.observation_written
    with factory() as session:
        observation = session.scalar(select(MonitoringObservation))
        assert observation.condition == "operational"
        assert observation.metadata_json["condition_source"] == "aggregate_power_generation"
        state = session.get(MonitoringCurrentState, ids["mapping"])
        assert state.latest_observation_id == observation.id


def test_night_reads_as_unknown_rather_than_offline(world) -> None:
    """The dongle states no condition at all, so zero power must not mean down.

    Otherwise every plant in the country would be reported as offline every
    single night.
    """
    factory, ids = world
    with_monitoring_policy(factory, ids)
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)

    ingestion.record_sample(
        session_id=session_id,
        binding=binding,
        outcome=outcome_at(datetime(2026, 8, 25, 2, 0, tzinfo=datetime_timezone.utc), pv=0, total=0),
    )

    with factory() as session:
        observation = session.scalar(select(MonitoringObservation))
        assert observation.condition == "unknown"
        # Still a complete reading: the plant answered, it just is not producing.
        assert observation.quality == "complete"


def test_polling_does_not_mint_a_monitoring_revision_every_cycle(world) -> None:
    """`observed_at` is this server's clock, so it differs on every poll."""
    factory, ids = world
    with_monitoring_policy(factory, ids)
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)
    start = datetime(2026, 8, 25, 12, 0, tzinfo=datetime_timezone.utc)

    for step in range(4):
        ingestion.record_sample(
            session_id=session_id, binding=binding, outcome=outcome_at(start + timedelta(seconds=30 * step))
        )

    with factory() as session:
        observations = list(session.scalars(select(MonitoringObservation)))
        assert len(observations) == 1


def test_a_mapping_the_policy_does_not_select_writes_samples_but_no_observation(world) -> None:
    """Samples are this provider's own evidence; current state follows policy.

    A plant watched by FusionSolar and metered by a dongle would otherwise
    have its "what is it doing now" decided by whichever answered last.
    """
    factory, ids = world  # no monitoring policy created
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)
    assert not binding.monitoring_selected

    result = ingestion.record_sample(
        session_id=session_id,
        binding=binding,
        outcome=outcome_at(datetime(2026, 8, 25, 12, 0, tzinfo=datetime_timezone.utc)),
    )

    assert result.created and not result.observation_written
    with factory() as session:
        assert session.scalar(select(MonitoringObservation)) is None


# --- session record, health, downstream probe ---------------------------------


def test_a_session_reports_under_a_sync_run_like_every_other_integration(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)
    ingestion.record_sample(
        session_id=session_id, binding=binding, outcome=outcome_at(datetime(2026, 8, 25, 12, 0, tzinfo=datetime_timezone.utc))
    )
    ingestion.close_session(session_id=session_id, reason="peer_closed")

    with factory() as session:
        run = session.scalar(select(SyncRun))
        assert run.capability == "current_monitoring"
        assert run.status == "success"
        assert run.metadata_json["transport"] == "inbound_tcp_session"
        assert run.metadata_json["items_accepted"] == 1
        health = session.get(IntegrationHealth, ids["connection"])
        assert health.sync_state == "healthy"
        assert health.last_successful_sync_at is not None


def test_a_session_that_produced_nothing_is_reported_as_failed(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, _binding = bind(ingestion)
    ingestion.record_poll_failure(
        session_id=session_id,
        outcome=PollOutcome(observed_at=datetime(2026, 8, 25, tzinfo=datetime_timezone.utc), error_code="timeout"),
    )
    ingestion.close_session(session_id=session_id, reason="read_error")

    with factory() as session:
        run = session.scalar(select(SyncRun))
        assert run.status == "failed"
        assert session.get(IntegrationHealth, ids["connection"]).sync_state == "degraded"


def test_a_session_with_samples_and_errors_is_partial_not_failed(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)
    ingestion.record_sample(
        session_id=session_id, binding=binding, outcome=outcome_at(datetime(2026, 8, 25, 12, 0, tzinfo=datetime_timezone.utc))
    )
    ingestion.record_poll_failure(
        session_id=session_id,
        outcome=PollOutcome(observed_at=datetime(2026, 8, 25, tzinfo=datetime_timezone.utc), error_code="timeout"),
    )
    ingestion.close_session(session_id=session_id, reason="peer_closed")

    with factory() as session:
        assert session.scalar(select(SyncRun)).status == "partial"


def test_the_inverter_refusal_is_recorded_as_evidence_not_as_a_failure(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, binding = bind(ingestion)

    ingestion.record_downstream_probe(
        session_id=session_id,
        probe=DownstreamProbe(unit_id=1, answered=False, exception_name="slave_device_failure", exception_code=0x04),
    )
    ingestion.record_sample(
        session_id=session_id, binding=binding, outcome=outcome_at(datetime(2026, 8, 25, 12, 0, tzinfo=datetime_timezone.utc))
    )
    ingestion.close_session(session_id=session_id, reason="peer_closed")

    with factory() as session:
        row = session.scalar(select(HuaweiScadaSession))
        assert row.metadata_json["downstream_probe"]["exception"] == "slave_device_failure"
        assert row.sample_count == 1
        # The refusal did not stop collection, and did not fail the run.
        assert session.scalar(select(SyncRun)).status == "success"


def test_a_session_a_crashed_listener_left_open_is_findable(world) -> None:
    factory, ids = world
    ingestion = ingestion_for(factory, ids)
    session_id, _binding = bind(ingestion)
    with factory() as session, session.begin():
        row = session.get(HuaweiScadaSession, session_id)
        row.last_seen_at = datetime(2026, 8, 20, tzinfo=datetime_timezone.utc)
        row.opened_at = datetime(2026, 8, 20, tzinfo=datetime_timezone.utc)

    with factory() as session:
        stale = stale_open_sessions(
            session, older_than=timedelta(hours=1), now=datetime(2026, 8, 25, tzinfo=datetime_timezone.utc)
        )
        assert [row.id for row in stale] == [session_id]


def test_quantising_puts_two_nearby_reads_in_one_bucket_and_distant_ones_apart() -> None:
    base = datetime(2026, 8, 25, 10, 0, 0, tzinfo=datetime_timezone.utc)
    assert quantise(base + timedelta(seconds=1), bucket_seconds=30) == base
    assert quantise(base + timedelta(seconds=29), bucket_seconds=30) == base
    assert quantise(base + timedelta(seconds=31), bucket_seconds=30) == base + timedelta(seconds=30)
