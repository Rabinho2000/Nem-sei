"""Contractual and operational availability coexisting for the same day.

The bug this file pins: every rollup used to read
`asset_availability_daily` as if `(asset_id, availability_date)` were the
key. It is not -- `uq_asset_availability_daily_day` includes `source`, on
purpose, so the contractual figure (`fusionsolar_device_history`) and the
operational one (`fusionsolar_sampled`) can both exist for one asset-day and
`select_availability` can choose between them.

The old `if len(rows) != len(asset_ids): return None` guard broke in two
directions at once:

* one asset with two sources looked like two assets, so a single-asset
  installation with both figures rolled up to `None`;
* two assets, one with two sources and one with none, produced a row count
  that *matched* -- so the guard passed, the doubled asset was weighted
  twice and the asset with no figure at all was silently dropped. That is
  a published percentage for an estate that was never fully measured, which
  is the exact failure mode the availability work exists to prevent.

Rows are written directly here rather than materialized. The materializers
have their own round-trip tests (`test_availability_service.py`,
`test_availability_contractual.py`); what needs proving here is the *read*
side's behaviour when both sources are present, and building a contractual
row through the slot engine would make each case a fixture exercise rather
than a statement about selection.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import permutations

import pytest

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.availability_service import (
    asset_availability_series,
    installation_availability_for_date,
    latest_closed_availability,
    portfolio_availability_for_date,
    selected_availability_by_asset,
)
from nemsei.diagnostics.models import AssetAvailabilityDaily
from nemsei.installations.models import Installation
from nemsei.portfolios.service import add_member, create_portfolio
from nemsei.reporting.rules.availability_source import (
    KIND_CONTRACTUAL,
    KIND_OPERATIONAL,
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
    SOURCE_FUSIONSOLAR_SAMPLED,
    SOURCE_MANUAL,
    select_availability,
)
from nemsei.shared.clock import utc_now
from tests_v2.test_migrations import upgrade


DAY = date(2026, 9, 6)


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _asset(session, *, power="100"):
    return create_asset(session, canonical_name=f"WAT fixture {power}", installed_dc_power_kw=Decimal(power))


def _availability_row(
    session,
    *,
    asset_id: int,
    source: str,
    pct,
    on: date = DAY,
    valid_sample_count: int = 47,
    expected_device_count: int = 8,
    observed_device_count: int = 8,
    coverage_status: str | None = None,
):
    """One stored day for one source.

    `coverage_status` follows the table's own CHECK
    (`ck_asset_availability_daily_pct_requires_complete`): a row carrying a
    percentage must be `complete`, and a row without one must not be.
    """
    now = utc_now()
    kind = KIND_CONTRACTUAL if source in (SOURCE_FUSIONSOLAR_DEVICE_HISTORY, SOURCE_MANUAL) else KIND_OPERATIONAL
    row = AssetAvailabilityDaily(
        asset_id=asset_id,
        availability_date=on,
        availability_pct=(Decimal(str(pct)) if pct is not None else None),
        valid_sample_count=valid_sample_count,
        expected_device_count=expected_device_count,
        observed_device_count=observed_device_count,
        minimum_required_samples=4,
        coverage_status=coverage_status or ("complete" if pct is not None else "indeterminate"),
        warning_codes_json=[],
        source=source,
        source_kind=kind,
        calculation_details_json={},
        calculated_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _installation_with(session, assets):
    now = utc_now()
    installation = Installation(display_name="WAT fixture installation", created_at=now, updated_at=now)
    session.add(installation)
    session.flush()
    for asset in assets:
        asset.installation_id = installation.id
    session.flush()
    return installation


# ---------------------------------------------------------------------------
# One asset, every combination of sources it can hold for a day.
# ---------------------------------------------------------------------------


def test_contractual_only_is_reported_as_contractual(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        session.commit()

        chosen = selected_availability_by_asset(session, asset_ids=[asset.id], target_date=DAY)[asset.id]
        assert chosen["availability_pct"] == pytest.approx(98.72)
        assert chosen["source"] == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
        assert chosen["source_kind"] == KIND_CONTRACTUAL


def test_operational_only_is_reported_and_stays_labelled_operational(settings, monkeypatch):
    """The figure is usable; what must never happen is it being *called* a WAT."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        session.commit()

        chosen = selected_availability_by_asset(session, asset_ids=[asset.id], target_date=DAY)[asset.id]
        assert chosen["availability_pct"] == pytest.approx(91.40)
        assert chosen["source"] == SOURCE_FUSIONSOLAR_SAMPLED
        assert chosen["source_kind"] == KIND_OPERATIONAL


def test_contractual_wins_over_operational_for_the_same_day(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        session.commit()

        chosen = selected_availability_by_asset(session, asset_ids=[asset.id], target_date=DAY)[asset.id]
        assert chosen["source_kind"] == KIND_CONTRACTUAL
        assert chosen["availability_pct"] == pytest.approx(98.72)


def test_contractual_without_a_percentage_never_promotes_the_operational_one(settings, monkeypatch):
    """A contractual row with `availability_pct=None` is coverage evidence.

    It does not block the operational figure from being reported (that would
    make an absent contractual day erase an operational one), and the
    operational figure that gets reported does not inherit the contractual
    label -- it is returned as what it is.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(
            session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct=None,
            observed_device_count=7, coverage_status="indeterminate",
        )
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        session.commit()

        chosen = selected_availability_by_asset(session, asset_ids=[asset.id], target_date=DAY)[asset.id]
        assert chosen["availability_pct"] == pytest.approx(91.40)
        assert chosen["source"] == SOURCE_FUSIONSOLAR_SAMPLED
        assert chosen["source_kind"] == KIND_OPERATIONAL


def test_no_availability_row_at_all_is_none_not_zero(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        session.commit()
        assert selected_availability_by_asset(session, asset_ids=[asset.id], target_date=DAY)[asset.id] is None


def test_rows_with_no_figure_at_all_report_the_contractual_evidence(settings, monkeypatch):
    """Both sources materialized, neither produced a number.

    The asset is not "unmeasured" -- it was measured and came out
    indeterminate -- so the caller gets the contractual row back (that is
    the number a report would have used, so its account of why it is absent
    is the useful one), with `availability_pct` still `None`.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct=None, coverage_status="partial")
        _availability_row(
            session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct=None,
            observed_device_count=7, coverage_status="indeterminate",
        )
        session.commit()

        chosen = selected_availability_by_asset(session, asset_ids=[asset.id], target_date=DAY)[asset.id]
        assert chosen is not None
        assert chosen["availability_pct"] is None
        assert chosen["source"] == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
        assert chosen["observed_device_count"] == 7


# ---------------------------------------------------------------------------
# Determinism: the answer cannot depend on the order rows arrive in.
# ---------------------------------------------------------------------------


def test_selection_is_independent_of_candidate_order():
    """Directly against the policy, over every ordering of three sources.

    The database returns rows in whatever order it likes; a report that
    changed number because a row was inserted later is not reproducible.
    """
    candidates = [
        {"source": SOURCE_FUSIONSOLAR_SAMPLED, "source_kind": KIND_OPERATIONAL, "availability_pct": 91.40},
        {"source": SOURCE_FUSIONSOLAR_DEVICE_HISTORY, "source_kind": KIND_CONTRACTUAL, "availability_pct": 98.72},
        {"source": SOURCE_MANUAL, "source_kind": KIND_CONTRACTUAL, "availability_pct": 99.90},
    ]
    outcomes = {select_availability(list(order))["source"] for order in permutations(candidates)}
    # `manual` outranks a derived contractual figure inside its own kind; the
    # point of the assertion is that there is exactly one answer, not six.
    assert outcomes == {SOURCE_MANUAL}


def test_stored_row_order_does_not_change_the_selected_figure(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        first = _asset(session, power="50")
        second = _asset(session, power="60")
        # Same two sources for both assets, inserted in opposite orders.
        _availability_row(session, asset_id=first.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        _availability_row(session, asset_id=first.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        _availability_row(session, asset_id=second.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        _availability_row(session, asset_id=second.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        session.commit()

        selected = selected_availability_by_asset(session, asset_ids=[first.id, second.id], target_date=DAY)
        assert selected[first.id]["source"] == selected[second.id]["source"] == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
        assert selected[first.id]["availability_pct"] == selected[second.id]["availability_pct"]


# ---------------------------------------------------------------------------
# Rollups: the bug's actual blast radius.
# ---------------------------------------------------------------------------


def test_installation_rollup_survives_two_sources_on_one_asset(settings, monkeypatch):
    """The regression: this returned `None` because two rows looked like two assets."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session, power="100")
        installation = _installation_with(session, [asset])
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        session.commit()

        rolled = installation_availability_for_date(session, installation_id=installation.id, target_date=DAY)
        assert rolled == pytest.approx(98.72)


def test_installation_rollup_with_two_assets_and_two_sources_each(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        heavy = _asset(session, power="90")
        light = _asset(session, power="10")
        installation = _installation_with(session, [heavy, light])
        _availability_row(session, asset_id=heavy.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="100.00")
        _availability_row(session, asset_id=heavy.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="50.00")
        _availability_row(session, asset_id=light.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="80.00")
        _availability_row(session, asset_id=light.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="10.00")
        session.commit()

        rolled = installation_availability_for_date(session, installation_id=installation.id, target_date=DAY)
        # Contractual figures only, weighted by installed power: (100*90 + 80*10)/100.
        assert rolled == pytest.approx(98.0)


def test_installation_rollup_is_none_when_one_member_has_no_row(settings, monkeypatch):
    """The other half of the old bug: the row count matched by coincidence.

    One asset with two sources plus one asset with none is two rows for two
    assets, so the old guard passed and published a figure that weighted the
    first asset twice and ignored the second entirely.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        measured = _asset(session, power="90")
        unmeasured = _asset(session, power="10")
        installation = _installation_with(session, [measured, unmeasured])
        _availability_row(session, asset_id=measured.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="100.00")
        _availability_row(session, asset_id=measured.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="50.00")
        session.commit()

        assert installation_availability_for_date(session, installation_id=installation.id, target_date=DAY) is None


def test_installation_rollup_is_none_when_a_member_has_rows_but_no_figure(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        measured = _asset(session, power="90")
        indeterminate = _asset(session, power="10")
        installation = _installation_with(session, [measured, indeterminate])
        _availability_row(session, asset_id=measured.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="100.00")
        _availability_row(session, asset_id=indeterminate.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct=None, coverage_status="indeterminate")
        session.commit()

        assert installation_availability_for_date(session, installation_id=installation.id, target_date=DAY) is None


def test_portfolio_rollup_survives_two_sources_per_member(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        heavy = _asset(session, power="90")
        light = _asset(session, power="10")
        portfolio = create_portfolio(session, name="WAT fixture portfolio", created_by="test-fixture")
        for asset in (heavy, light):
            add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="test-fixture")
        _availability_row(session, asset_id=heavy.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="100.00")
        _availability_row(session, asset_id=heavy.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="50.00")
        _availability_row(session, asset_id=light.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="80.00")
        _availability_row(session, asset_id=light.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="10.00")
        session.commit()

        assert portfolio_availability_for_date(session, portfolio_id=portfolio.id, target_date=DAY) == pytest.approx(98.0)


# ---------------------------------------------------------------------------
# The daily series the installation page reads.
# ---------------------------------------------------------------------------


def test_series_covers_every_day_and_leaves_gaps_as_gaps(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72", on=DAY)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="97.00", on=date(2026, 9, 4))
        session.commit()

        series = asset_availability_series(session, asset_id=asset.id, from_date=date(2026, 9, 3), to_date=DAY)
        assert [entry["date"] for entry in series] == [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5), DAY]
        assert [entry["availability_pct"] for entry in series] == [None, pytest.approx(97.0), None, pytest.approx(98.72)]
        # A day with no row is missing, never a zero, and carries no source.
        assert series[0]["coverage_status"] == "missing"
        assert series[0]["source"] is None
        assert series[2]["availability_pct"] is None


def test_series_labels_each_day_with_the_source_it_came_from(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40", on=date(2026, 9, 5))
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="90.00", on=DAY)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72", on=DAY)
        session.commit()

        series = asset_availability_series(session, asset_id=asset.id, from_date=date(2026, 9, 5), to_date=DAY)
        assert [entry["source_kind"] for entry in series] == [KIND_OPERATIONAL, KIND_CONTRACTUAL]
        assert series[1]["sources_available"] == [SOURCE_FUSIONSOLAR_DEVICE_HISTORY, SOURCE_FUSIONSOLAR_SAMPLED]


def test_latest_closed_availability_skips_days_without_a_figure(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        _availability_row(session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72", on=date(2026, 9, 4))
        _availability_row(
            session, asset_id=asset.id, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct=None, on=DAY,
            observed_device_count=7, coverage_status="indeterminate",
        )
        session.commit()

        latest = latest_closed_availability(session, asset_id=asset.id, on_or_before=DAY)
        assert latest["date"] == date(2026, 9, 4)
        assert latest["availability_pct"] == pytest.approx(98.72)


def test_latest_closed_availability_is_none_when_nothing_was_ever_measured(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        asset = _asset(session)
        session.commit()
        assert latest_closed_availability(session, asset_id=asset.id, on_or_before=DAY) is None
