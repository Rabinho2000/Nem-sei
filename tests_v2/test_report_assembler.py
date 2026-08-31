"""The assembly layer: database rows in, a renderable report payload out.

These tests exist because the renderers were already proven against V1 while
nothing produced their input from V2. They therefore assert three things: that a
correction never inflates a total, that an absent measurement stays absent all
the way into the rendered document, and that both renderers accept what the
assembler builds.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.assembler import (
    assemble_asset_report,
    excel_payload_from_report,
)
from nemsei.reporting.customer_pdf import build_customer_report_pdf
from nemsei.reporting.datasets import build_dataset, rehydrate_snapshot_payload, snapshot_dataset
from nemsei.reporting.excel import build_asset_report_workbook
from nemsei.reporting.periods import build_period, monthly_period
from nemsei.reporting.rules.types import ReportPeriodType


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


@pytest.fixture
def prepared(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key="c1",
            display_name="C1",
            credential_reference="REF",
            enabled=True,
            configuration_status="configured",
        )
        mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=157795675"
        )
        ids = (asset.id, mapping.id)
    return factory, ids


def add_fact(session, asset_id, mapping_id, day: date, value, *, key=None, quality="complete"):
    return record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=key or f"pvyield:{day.isoformat()}",
        period_start=utc(day),
        period_end=utc(date.fromordinal(day.toordinal() + 1)),
        granularity="day",
        value=None if value is None else Decimal(str(value)),
        unit="kWh",
        quality=quality,
        completeness="complete" if quality == "complete" else "partial",
        metadata={},
    )


def add_whole_month(session, asset_id, mapping_id, year: int, month: int, value="10"):
    """Every day of a month, so the period is genuinely final.

    A test that wants to exercise what a *closed* report says has to say so with
    days, because that is what closes a period now (`reporting/finality.py`).
    Before, one fact in a month was enough to make `production_is_final` true,
    and several tests were unknowingly asserting against a report that claimed
    to be final on a thirty-first of the evidence.
    """
    from calendar import monthrange

    for day in range(1, monthrange(year, month)[1] + 1):
        add_fact(session, asset_id, mapping_id, date(year, month, day), value)


def test_a_corrected_reading_replaces_the_original_instead_of_adding_to_it(prepared) -> None:
    """The shape of the only real data V2 holds: one day, corrected once.

    `production_facts` is append-only, so the correction sits beside the value it
    replaced. Totalling the raw rows would report 129.28 kWh for a day that
    produced 59.56.
    """
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 7, 24), "69.72")
        add_fact(session, asset_id, mapping_id, date(2026, 7, 24), "59.56")
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )

    assert assembled.payload["production_kwh"] == pytest.approx(59.56)
    assert assembled.payload["production_kwh"] != pytest.approx(129.28)
    assert len(assembled.payload["daily_rows"]) == 1
    assert assembled.payload["daily_rows"][0]["production_kwh"] == pytest.approx(59.56)


def test_a_superseded_revision_does_not_inflate_the_dataset_row(prepared) -> None:
    """The same rule at the dataset level, which is what a snapshot freezes."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 7, 24), "69.72")
        add_fact(session, asset_id, mapping_id, date(2026, 7, 24), "59.56")
        dataset = build_dataset(
            session,
            asset_id=asset_id,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 8, 1),
            built_by="operator",
        )
        row = dataset.rows[0]
        assert row.actual_production_kwh == Decimal("59.560000")
        assert row.actual_state == "measured"


def test_an_unsourced_field_is_absent_rather_than_zero(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        # A complete July, so this is testing what an unsourced metric does in a
        # *final* report rather than what a provisional one withholds.
        add_whole_month(session, asset_id, mapping_id, 2026, 7, "59.56")
        assembled = assemble_asset_report(
            session,
            asset_id=asset_id,
            period=monthly_period("2026-07"),
            built_by="operator",
            today=date(2026, 8, 31),
        )
    assert assembled.payload["reporting_state"] == "final"

    for name in ("self_use_kwh", "export_kwh", "consumption_kwh"):
        assert assembled.payload[name] is None, f"{name} must stay missing, not become zero"
        assert name in assembled.unavailable_fields
    # `grid_import_kwh` is the exception, and deliberately so. V1 derives it
    # inside `prepare_customer_report` as consumption minus self-use whenever the
    # payload does not state it, so two absent inputs produce a real 0.0 rather
    # than a missing value. Changing that would break renderer parity, so the
    # field is still declared unavailable while carrying V1's derived figure.
    assert assembled.payload["grid_import_kwh"] == 0.0
    assert "grid_import_kwh" in assembled.unavailable_fields
    # And the gap is declared on the payload itself, not only on the result.
    assert "tariff_value_eur" in assembled.payload["unavailable_fields"]
    assert "asset.contract_type" in assembled.payload["unavailable_fields"]


def test_a_month_short_of_days_states_no_money_at_all(prepared) -> None:
    """Expertcom's August, in miniature: 26 of 31 days, five of them absent.

    The energy is reported, because it was measured. Every euro is withheld,
    because a customer is not invoiced from five-sixths of a month -- and the
    difference between "0,00 €" and "not calculable yet" is the whole point.
    """
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        for day in range(1, 32):
            if day in {19, 20, 21, 22, 23}:
                continue
            add_fact(session, asset_id, mapping_id, date(2026, 8, day), "100")
        assembled = assemble_asset_report(
            session,
            asset_id=asset_id,
            period=monthly_period("2026-08"),
            built_by="operator",
            today=date(2026, 9, 4),
        )

    payload = assembled.payload
    assert payload["reporting_state"] == "provisional"
    assert payload["production_is_final"] is False
    assert payload["observed_source_days"] == 26
    assert payload["expected_source_days"] == 31
    assert payload["missing_source_days"] == [f"2026-08-{day}" for day in range(19, 24)]
    # Measured energy still reaches the report.
    assert payload["production_kwh"] == pytest.approx(2600.0)
    # No euro does.
    for name in ("savings_eur", "export_revenue_eur", "solcor_payment_eur", "net_benefit_eur"):
        assert payload[name] is None, f"{name} must not be stated for a provisional month"


def test_the_last_day_of_a_running_month_is_provisional_even_when_nothing_is_missing(prepared) -> None:
    """The rule the calendar alone would get wrong, in the direction that matters."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_whole_month(session, asset_id, mapping_id, 2026, 8, "100")
        running = assemble_asset_report(
            session,
            asset_id=asset_id,
            period=monthly_period("2026-08"),
            built_by="operator",
            today=date(2026, 8, 31),
        )
        closed = assemble_asset_report(
            session,
            asset_id=asset_id,
            period=monthly_period("2026-08"),
            built_by="operator",
            today=date(2026, 9, 1),
        )

    assert running.payload["reporting_state"] == "provisional"
    assert "period_still_open" in running.payload["reporting_state_reasons"]
    assert closed.payload["reporting_state"] == "final"
    # Same facts, different answer, therefore a different report identity.
    assert running.payload["production_kwh"] == closed.payload["production_kwh"]
    assert running.payload["savings_eur"] is None
    assert closed.payload["savings_eur"] is not None


def test_a_real_zero_is_kept_apart_from_a_missing_month(prepared) -> None:
    """Zero production and unknown production are different customer statements."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 7, 24), "0")
        zero = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        absent = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-06"), built_by="operator"
        )

    assert zero.payload["production_kwh"] == 0.0
    assert zero.payload["production_quality_status"] == "complete"
    assert absent.payload["production_kwh"] is None
    assert absent.payload["production_quality_status"] == "missing"
    assert absent.payload["missing_months"] == ["2026-06"]


def test_coverage_reports_the_months_that_actually_have_data(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 7, 10), "100")
        add_fact(session, asset_id, mapping_id, date(2026, 9, 10), "200")
        assembled = assemble_asset_report(
            session,
            asset_id=asset_id,
            period=build_period(ReportPeriodType.QUARTERLY, year=2026, quarter=3),
            built_by="operator",
        )

    payload = assembled.payload
    assert payload["months_count"] == 3
    assert payload["months_with_data"] == ["2026-07", "2026-09"]
    assert payload["missing_months"] == ["2026-08"]
    assert payload["coverage_pct"] == pytest.approx(200 / 3)
    assert payload["production_kwh"] == pytest.approx(300.0)
    # A period missing a month is never final, however much data the rest holds.
    assert payload["production_is_final"] is False
    assert payload["chart_granularity"] == "monthly"
    assert payload["period_label"] == "T3 2026"


def test_the_period_end_crosses_from_inclusive_to_exclusive_without_losing_a_day(prepared) -> None:
    """V1's period end is the last day; the dataset's is the day after it.

    A fact on the final day of the month must land inside the period. Getting
    this wrong silently drops one day from every report.
    """
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 7, 31), "17.5")
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )

    assert assembled.payload["period_end"] == date(2026, 7, 31)
    assert assembled.payload["production_kwh"] == pytest.approx(17.5)
    assert [row["date"] for row in assembled.payload["daily_rows"]] == [date(2026, 7, 31)]


def test_the_payload_carries_the_provenance_of_the_dataset_it_came_from(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 7, 24), "59.56")
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        assert assembled.payload["dataset_id"] == assembled.dataset.id
        assert assembled.payload["dataset_input_digest"] == assembled.dataset.input_digest
        assert len(assembled.dataset.input_digest) == 64
        assert assembled.payload["asset"]["external_id"] == "NE=157795675"
        assert assembled.payload["asset"]["energy_provider"] == "fusionsolar"
        assert assembled.payload["station_code"] == "NE=157795675"


def test_both_renderers_accept_what_the_assembler_builds(prepared) -> None:
    """End to end from the database: rows in, a PDF and a workbook out."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        for day, value in ((date(2026, 7, 10), "40"), (date(2026, 7, 11), "60")):
            add_fact(session, asset_id, mapping_id, day, value)
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        payload = assembled.payload

    pdf_bytes = build_customer_report_pdf(payload)
    assert pdf_bytes[:5] == b"%PDF-"
    assert len(pdf_bytes) > 1000

    workbook = build_asset_report_workbook(excel_payload_from_report(payload))
    assert workbook.sheetnames == ["Resumo", "Energia", "Financeiro", "Qualidade dos dados", "Metadados"]
    energy = workbook["Energia"]
    values = {energy.cell(row, 1).value: energy.cell(row, 2).value for row in range(3, 8)}
    assert values["production_kwh"] == "100.00"
    # The unsourced series render as an empty cell, never as 0.00.
    assert values["self_use_kwh"] is None
    assert values["export_kwh"] is None

    stream = io.BytesIO()
    workbook.save(stream)
    assert stream.getbuffer().nbytes > 0


def test_a_zero_percent_donut_renders_instead_of_crashing(prepared) -> None:
    """V1 dies here; V2 must not.

    `_draw_donut` passes its percentage to reportlab as an angular extent, and an
    extent of exactly zero raises ZeroDivisionError inside `pdfgeom.bezierArc`.
    V1 never reached it because its payloads always carried a non-zero self-use
    figure. Every V2-assembled report does, because self-consumption has no
    persisted source and is derived as zero.
    """
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_whole_month(session, asset_id, mapping_id, 2026, 7, "100")
        assembled = assemble_asset_report(
            session,
            asset_id=asset_id,
            period=monthly_period("2026-07"),
            built_by="operator",
            today=date(2026, 8, 31),
        )
        payload = assembled.payload

    # The zero arrives through self-sufficiency: self-use over a consumption
    # V2 cannot source, so the ratio is a real 0 % rather than an unknown. It
    # only reaches the payload at all on a final period -- a provisional one
    # withholds every ratio along with the money -- so the month is complete.
    assert payload["reporting_state"] == "final"
    assert payload["self_sufficiency_pct"] == 0
    pdf_bytes = build_customer_report_pdf(payload)
    assert pdf_bytes[:5] == b"%PDF-"


def test_a_real_assembled_payload_can_actually_be_frozen(prepared) -> None:
    """`assemble_asset_report`'s payload carries real `date` objects.

    `snapshot_dataset` stores it in a JSON column, which has no idea how to
    serialize a `date` and previously raised `TypeError` on every real payload —
    `report_snapshots` held zero rows in production for exactly this reason.
    Freezing an individual report never actually worked end to end until this
    was fixed at the source, in `snapshot_dataset` itself.
    """
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        for day, value in ((date(2026, 7, 10), "40"), (date(2026, 7, 11), "60")):
            add_fact(session, asset_id, mapping_id, day, value)
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        snapshot = snapshot_dataset(
            session, dataset=assembled.dataset, payload=assembled.payload, created_by="operator"
        )
        snapshot_id = snapshot.id
        digest = snapshot.snapshot_digest

    with factory() as session:
        from nemsei.reporting.models import ReportSnapshot

        stored = session.get(ReportSnapshot, snapshot_id)
        assert stored.snapshot_digest == digest
        # Stored as ISO strings: readable outside Python, and what makes the
        # JSON column accept it in the first place.
        assert stored.payload_json["period_start"] == "2026-07-01"
        assert isinstance(stored.payload_json["daily_rows"][0]["date"], str)
        # `prepare_customer_report` always adds tariff_rows carrying a
        # reportlab Color; that is the other value the JSON column rejects.
        assert isinstance(stored.payload_json["tariff_rows"][0][2], str)
        assert stored.payload_json["tariff_rows"][0][2].startswith("0x")


def test_regenerating_an_unchanged_period_reuses_the_same_snapshot(prepared) -> None:
    """"Re-freezing identical input reuses it" is the docstring's promise.

    `build_dataset` is never deduplicated — two calls over the same facts
    produce two different `ReportingDataset` rows — and `assemble_asset_report`
    embeds that row's id in the payload. Hashing the id along with everything
    else meant no real payload could ever match a prior one: every
    regeneration looked new even when nothing about the facts had changed.
    """
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add_fact(session, asset_id, mapping_id, date(2026, 7, 10), "40")
        first_assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        first = snapshot_dataset(
            session, dataset=first_assembled.dataset, payload=first_assembled.payload, created_by="operator"
        )
        first_id = first.id

    with factory() as session, session.begin():
        # A fresh build of the same unchanged facts: a new ReportingDataset row,
        # a new dataset_id in the payload, but the same content.
        second_assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        assert second_assembled.dataset.id != first_assembled.dataset.id
        second = snapshot_dataset(
            session, dataset=second_assembled.dataset, payload=second_assembled.payload, created_by="operator"
        )

    assert second.id == first_id


def test_a_reopened_snapshot_still_draws_its_daily_chart(prepared) -> None:
    """The one field storage cannot round-trip losslessly: `daily_rows[].date`.

    `customer_pdf.py` keeps a daily row only when `isinstance(row["date"], date)`
    holds. Reopen a snapshot without rehydrating that field and every daily row
    is silently dropped — the frozen record of what a customer was told would
    render with an empty chart, which defeats the entire point of freezing it.
    """
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        for day, value in ((date(2026, 7, 10), "40"), (date(2026, 7, 11), "60")):
            add_fact(session, asset_id, mapping_id, day, value)
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        live_pdf = build_customer_report_pdf(assembled.payload)
        snapshot = snapshot_dataset(
            session, dataset=assembled.dataset, payload=assembled.payload, created_by="operator"
        )
        snapshot_id = snapshot.id

    with factory() as session:
        from nemsei.reporting.models import ReportSnapshot

        stored = session.get(ReportSnapshot, snapshot_id)
        rehydrated = rehydrate_snapshot_payload(stored.payload_json)

    assert [row["date"] for row in rehydrated["daily_rows"]] == [date(2026, 7, 10), date(2026, 7, 11)]
    # The colour that decides the donut chart's theming survives the round
    # trip as the identical Color, not a lookalike with a different identity.
    from reportlab.lib.colors import Color

    assert isinstance(rehydrated["tariff_rows"][0][2], Color)
    assert rehydrated["tariff_rows"][0][2] == assembled.payload["tariff_rows"][0][2]

    reopened_pdf = build_customer_report_pdf(rehydrated)
    assert reopened_pdf[:5] == b"%PDF-"
    # A page that silently dropped its chart is a materially smaller document,
    # not merely a byte-identical one (reportlab stamps a creation time).
    assert len(reopened_pdf) > len(live_pdf) * 0.9
