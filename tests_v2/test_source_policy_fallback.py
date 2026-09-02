"""A fallback source policy is a real, deliberate selection outcome now.

Before 2026-09-02, `is_fallback=True` policies were stored, validated, and
shown in the web UI, but `resolve_source_policy` never once consulted them --
the field was inert. Motivated by a genuinely separate FusionSolar account
(`om_api2`, its own daily provider quota) able to see most of the same
station portfolio as the shared `primary` account, this makes the fallback
real: when a production day's primary mapping has *nothing* recorded for it
-- not a failed call, a persisted absence -- and the day is old enough that
the primary's own normal sync would already have tried it, a configured
fallback policy is selected instead.

Deliberately narrow: `source_use == "production"` only (the only use
`ProductionFact` covers), and gated by `_FALLBACK_GRACE_DAYS` so a live,
current-day incremental read is never redirected before the primary gets its
own chance to run.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy, resolve_source_policy
from tests_v2.test_migrations import upgrade


EARLY = date(2020, 1, 1)


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def old_enough_day() -> date:
    """Well past `_FALLBACK_GRACE_DAYS`, however slowly this suite runs."""
    return datetime.now(timezone.utc).date() - timedelta(days=10)


def world(factory, *, fallback_count: int = 1):
    """One asset, a primary mapping/policy, and `fallback_count` fallback
    mappings/policies -- each on its own connection, the way a genuinely
    separate provider account would be."""
    with factory() as session:
        primary_conn = create_connection(
            session, provider_code="fusionsolar", connection_key="primary-conn",
            display_name="Primary", credential_reference="primary", enabled=True, configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Fallback test asset")
        session.flush()
        primary_mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=primary_conn.id, external_id="NE=PRIMARY", valid_from=EARLY,
        )
        session.flush()
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=primary_mapping.id,
            source_use="production", priority=1, valid_from=EARLY,
        )
        fallback_mappings = []
        for index in range(fallback_count):
            conn = create_connection(
                session, provider_code="fusionsolar", connection_key=f"fallback-conn-{index}",
                display_name=f"Fallback {index}", credential_reference=f"fallback{index}",
                enabled=True, configuration_status="configured",
            )
            mapping = create_mapping(
                session, asset_id=asset.id, provider_connection_id=conn.id, external_id=f"NE=FALLBACK{index}", valid_from=EARLY,
            )
            session.flush()
            create_source_policy(
                session, asset_id=asset.id, provider_mapping_id=mapping.id,
                source_use="production", priority=index + 1, valid_from=EARLY, is_fallback=True,
            )
            fallback_mappings.append(mapping)
        session.commit()
        return asset.id, primary_mapping, fallback_mappings


def record(factory, *, asset_id, mapping_id, on_date, quality="complete", value="10.0"):
    start = datetime.combine(on_date, time.min, tzinfo=timezone.utc)
    with factory() as session:
        record_production_fact(
            session,
            asset_id=asset_id,
            provider_mapping_id=mapping_id,
            source_fact_key=f"test-fallback:{mapping_id}:{on_date.isoformat()}",
            period_start=start,
            period_end=start + timedelta(days=1),
            granularity="day",
            value=None if quality == "missing" else value,
            unit="kWh",
            quality=quality,
            completeness="complete" if quality == "complete" else "partial",
        )
        session.commit()


def test_fallback_is_selected_when_the_primary_has_nothing_recorded(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    asset_id, primary, fallbacks = world(factory)
    day = old_enough_day()
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=day)
    assert resolved.provider_mapping_id == fallbacks[0].id


def test_the_primary_still_wins_once_it_has_recorded_anything(settings, monkeypatch):
    """Including a 'missing' fact -- that is an answer, not an absence."""
    factory = factory_for(settings, monkeypatch)
    asset_id, primary, fallbacks = world(factory)
    day = old_enough_day()
    record(factory, asset_id=asset_id, mapping_id=primary.id, on_date=day, quality="missing")
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=day)
    assert resolved.provider_mapping_id == primary.id


def test_a_complete_primary_fact_also_keeps_the_primary(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    asset_id, primary, fallbacks = world(factory)
    day = old_enough_day()
    record(factory, asset_id=asset_id, mapping_id=primary.id, on_date=day, quality="complete")
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=day)
    assert resolved.provider_mapping_id == primary.id


@pytest.mark.parametrize("days_ago", [0, 1])
def test_the_grace_window_never_redirects_a_live_or_near_current_day(settings, monkeypatch, days_ago):
    """Today, and the day behind it, are the primary's own normal
    reconciliation window -- it has not necessarily run yet, and an absent
    fact there must never be mistaken for 'the primary has nothing'."""
    factory = factory_for(settings, monkeypatch)
    asset_id, primary, fallbacks = world(factory)
    day = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=day)
    assert resolved.provider_mapping_id == primary.id


def test_the_grace_window_boundary_is_the_first_eligible_day(settings, monkeypatch):
    """Exactly `_FALLBACK_GRACE_DAYS` ago is where eligibility starts."""
    factory = factory_for(settings, monkeypatch)
    asset_id, primary, fallbacks = world(factory)
    from nemsei.sources.service import _FALLBACK_GRACE_DAYS

    day = datetime.now(timezone.utc).date() - timedelta(days=_FALLBACK_GRACE_DAYS)
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=day)
    assert resolved.provider_mapping_id == fallbacks[0].id


def test_fallback_never_applies_to_monitoring(settings, monkeypatch):
    """ProductionFact is what the existence check reads; monitoring has no
    such durable per-day signal here, so fallback is scoped out entirely."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        primary_conn = create_connection(
            session, provider_code="fusionsolar", connection_key="mon-primary",
            display_name="Primary", credential_reference="primary", enabled=True, configuration_status="configured",
        )
        fallback_conn = create_connection(
            session, provider_code="fusionsolar", connection_key="mon-fallback",
            display_name="Fallback", credential_reference="fb", enabled=True, configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Monitoring fallback asset")
        session.flush()
        primary_mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=primary_conn.id, external_id="NE=MP", valid_from=EARLY)
        fallback_mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=fallback_conn.id, external_id="NE=MF", valid_from=EARLY)
        session.flush()
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=primary_mapping.id, source_use="monitoring", priority=1, valid_from=EARLY)
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=fallback_mapping.id, source_use="monitoring", priority=1, valid_from=EARLY, is_fallback=True)
        session.commit()
        asset_id = asset.id
        primary_id = primary_mapping.id
    day = old_enough_day()
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="monitoring", on_date=day)
    assert resolved.provider_mapping_id == primary_id


def test_the_lowest_priority_fallback_wins_when_several_exist(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    asset_id, primary, fallbacks = world(factory, fallback_count=3)
    day = old_enough_day()
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=day)
    # world() assigns priority=index+1, so fallbacks[0] (priority 1) must win
    # over fallbacks[1]/[2] (priority 2/3).
    assert resolved.provider_mapping_id == fallbacks[0].id


def test_no_primary_at_all_still_raises_even_with_a_fallback_configured(settings, monkeypatch):
    """A fallback substitutes for a primary with nothing recorded, never for
    an asset that was never given a primary policy in the first place --
    that is still a configuration gap, not a resolvable one."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        fallback_conn = create_connection(
            session, provider_code="fusionsolar", connection_key="orphan-fallback",
            display_name="Fallback", credential_reference="fb2", enabled=True, configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="No primary asset")
        session.flush()
        fallback_mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=fallback_conn.id, external_id="NE=ORPHAN", valid_from=EARLY)
        session.flush()
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=fallback_mapping.id, source_use="production", priority=1, valid_from=EARLY, is_fallback=True)
        session.commit()
        asset_id = asset.id
    with factory() as session, pytest.raises(ValueError, match="No primary source policy"):
        resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=old_enough_day())


def test_competing_primaries_still_raise_regardless_of_any_fallback(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    asset_id, primary, fallbacks = world(factory)
    with factory() as session:
        rival_conn = create_connection(
            session, provider_code="fusionsolar", connection_key="rival-primary",
            display_name="Rival", credential_reference="rival", enabled=True, configuration_status="configured",
        )
        rival_mapping = create_mapping(session, asset_id=asset_id, provider_connection_id=rival_conn.id, external_id="NE=RIVAL", valid_from=EARLY)
        session.flush()
        create_source_policy(session, asset_id=asset_id, provider_mapping_id=rival_mapping.id, source_use="production", priority=1, valid_from=EARLY)
        session.commit()
    with factory() as session, pytest.raises(ValueError, match="Competing primary"):
        resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=old_enough_day())


def test_without_any_fallback_configured_the_primary_is_returned_regardless(settings, monkeypatch):
    """No behavior change at all for the overwhelming majority of assets,
    which have no fallback policy: the primary is returned exactly as
    before, whether or not it has recorded anything."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        conn = create_connection(
            session, provider_code="fusionsolar", connection_key="solo-primary",
            display_name="Solo", credential_reference="solo", enabled=True, configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="No fallback configured")
        session.flush()
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=conn.id, external_id="NE=SOLO", valid_from=EARLY)
        session.flush()
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_use="production", priority=1, valid_from=EARLY)
        session.commit()
        asset_id, mapping_id = asset.id, mapping.id
    with factory() as session:
        resolved = resolve_source_policy(session, asset_id=asset_id, source_use="production", on_date=old_enough_day())
    assert resolved.provider_mapping_id == mapping_id
