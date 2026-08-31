"""The state an operator reads next to an installation's name.

Most of this needs no database: the decision is a pure function over the
evidence, so the cases that matter -- a working plant, a plant asleep at
night, a plant nobody has read in a month -- are stated directly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.service import record_device_status
from nemsei.monitoring.installation_state import (
    FRESH_WINDOW,
    classify_installation_state,
    current_installation_state,
    current_installation_states,
)


NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)


def ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


def decide(plant=None, plant_at=None, devices=(), now=NOW):
    return classify_installation_state(
        asset_id=1, plant_condition=plant, plant_observed_at=plant_at, device_readings=list(devices), now=now
    )


# --- the pure decision --------------------------------------------------------


def test_nothing_ever_read_is_not_the_same_answer_as_a_reading_that_says_nothing():
    assert decide().state == "no_evidence"
    assert decide(plant="unknown", plant_at=ago(minutes=5)).state == "unknown"


def test_a_provider_saying_the_plant_is_operational_is_believed():
    state = decide(plant="operational", plant_at=ago(minutes=10))
    assert state.state == "operational" and state.source == "plant_observation"


@pytest.mark.parametrize(("condition", "expected"), [("fault", "fault"), ("offline", "offline"), ("warning", "warning")])
def test_every_stated_plant_condition_maps_through(condition, expected):
    assert decide(plant=condition, plant_at=ago(minutes=10)).state == expected


def test_devices_answer_when_the_plant_endpoint_states_nothing():
    """The Sigenergy and FusionSolar-device case: no plant status, real inverters."""
    state = decide(plant="unknown", plant_at=ago(minutes=10), devices=[("available", ago(minutes=8))])
    assert state.state == "operational" and state.source == "device_facts"
    assert state.detail == "1 de 1 equipamentos disponíveis"


def test_one_working_inverter_makes_the_installation_operational():
    state = decide(devices=[("available", ago(minutes=5)), ("unavailable", ago(minutes=5))])
    assert state.state == "operational" and state.detail == "1 de 2 equipamentos disponíveis"


def test_every_inverter_down_is_a_fault_not_an_unknown():
    state = decide(devices=[("unavailable", ago(minutes=5)), ("unavailable", ago(minutes=5))])
    assert state.state == "fault"


def test_every_inverter_at_rest_is_standby_not_a_problem():
    """Night. The plant is fine and not generating, and must not read as broken."""
    state = decide(devices=[("standby", ago(minutes=5)), ("standby", ago(minutes=5))])
    assert state.state == "standby" and state.tone == "muted"


def test_only_unknown_device_readings_stay_unknown():
    assert decide(devices=[("unknown", ago(minutes=5))]).state == "unknown"


def test_evidence_older_than_the_window_is_stale_rather_than_a_current_answer():
    """The V1 import's ceiling: a July reading must not describe August."""
    state = decide(plant="operational", plant_at=ago(days=40), devices=[("available", ago(days=40))])
    assert state.state == "stale"
    assert state.detail is not None and "2026-07-16" in state.detail


def test_an_unchanged_plant_still_being_read_is_not_stale():
    """Found live: an operational plant keeps its observation row for days.

    Freshness has to come from the confirmation clock, not from the
    observation, or every healthy installation goes "sem leitura recente"
    within a day of its last state change while being read every 15 minutes.
    """
    state = classify_installation_state(
        asset_id=1, plant_condition="operational", plant_observed_at=ago(days=3),
        plant_confirmed_at=ago(minutes=6), device_readings=[], now=NOW,
    )
    assert state.state == "operational"
    assert state.observed_at == ago(minutes=6), "shown as of the last confirmation"
    assert state.since == ago(days=3), "and operational since the state actually changed"


def test_a_plant_whose_reads_stopped_is_stale_even_with_a_recent_condition():
    state = classify_installation_state(
        asset_id=1, plant_condition="operational", plant_observed_at=ago(hours=30),
        plant_confirmed_at=ago(hours=30), device_readings=[], now=NOW,
    )
    assert state.state == "stale"


def test_the_freshness_boundary_is_the_declared_window():
    inside = decide(devices=[("available", NOW - FRESH_WINDOW)])
    outside = decide(devices=[("available", NOW - FRESH_WINDOW - timedelta(seconds=1))])
    assert inside.state == "operational" and outside.state == "stale"


def test_a_stale_plant_reading_does_not_hide_a_fresh_device_reading():
    state = decide(plant="offline", plant_at=ago(days=30), devices=[("available", ago(minutes=5))])
    assert state.state == "operational" and state.source == "device_facts"


# --- against the database -----------------------------------------------------


@pytest.fixture
def factory(settings, monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")
    return build_session_factory(create_engine(settings.database_url))


def test_an_installation_with_no_facts_at_all_reads_no_evidence(factory):
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Sem leituras")
        asset_id = asset.id
    with factory() as session:
        state = current_installation_state(session, asset_id=asset_id, now=NOW)
    assert state.state == "no_evidence" and state.label == "Sem leitura"


def test_the_newest_device_reading_of_each_device_decides_the_installation(factory):
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Com inversores")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        asset_id, device_id = asset.id, device.id
    with factory() as session, session.begin():
        # An older reading that said the inverter was down, then a newer one
        # saying it recovered. The newest wins; the older row stays a row.
        record_device_status(session, device_id=device_id, asset_id=asset_id, source_fact_key="old", observed_at=ago(hours=3), availability_status="unavailable")
        record_device_status(session, device_id=device_id, asset_id=asset_id, source_fact_key="new", observed_at=ago(minutes=20), availability_status="available")
    with factory() as session:
        state = current_installation_state(session, asset_id=asset_id, now=NOW)
    assert state.state == "operational" and state.observed_at == ago(minutes=20)


def test_states_are_returned_for_every_asset_asked_for_including_empty_ones(factory):
    with factory() as session, session.begin():
        working = create_asset(session, canonical_name="A trabalhar")
        silent = create_asset(session, canonical_name="Sem nada")
        device = create_device(session, asset_id=working.id, device_kind="inverter", label="INV-1", valid_from=date(2026, 1, 1))
        working_id, silent_id, device_id = working.id, silent.id, device.id
    with factory() as session, session.begin():
        record_device_status(session, device_id=device_id, asset_id=working_id, source_fact_key="k", observed_at=ago(minutes=5), availability_status="available")
    with factory() as session:
        states = current_installation_states(session, asset_ids=[working_id, silent_id], now=NOW)
    assert states[working_id].state == "operational"
    assert states[silent_id].state == "no_evidence"


def test_asking_for_nothing_costs_no_query(factory):
    with factory() as session:
        assert current_installation_states(session, asset_ids=[], now=NOW) == {}
