"""The inputs a complete report needs: energy metrics, tariffs, billing terms.

These assert the three properties that decide whether a number reaches a
customer correctly: that a metric with no fact stays missing rather than
becoming zero, that only one tariff can ever price a given day, and that a
report type is only claimed when the contract actually says so.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from nemsei.assets.service import create_asset
from nemsei.db.session import build_session_factory
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.assembler import assemble_asset_report, excel_payload_from_report
from nemsei.reporting.commercial import (
    report_type_is_resolved,
    resolve_billing_config,
    resolve_tariff,
    set_billing_config,
    set_tariff,
)
from nemsei.reporting.customer_pdf import build_customer_report_pdf
from nemsei.reporting.excel import build_asset_report_workbook
from nemsei.reporting.periods import monthly_period
from nemsei.reporting.rules.v1_energy_signals import identity_violations, metrics_from_row


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


@pytest.fixture
def prepared(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Alpha Solar")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="C1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        mapping = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id, external_id="NE=1"
        )
        ids = (asset.id, mapping.id)
    return factory, ids


def add(session, asset_id, mapping_id, day: date, metric: str, value, *, quality="complete"):
    return record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"{metric}:{day.isoformat()}",
        period_start=utc(day),
        period_end=utc(date.fromordinal(day.toordinal() + 1)),
        granularity="day",
        metric_kind=metric,
        value=None if value is None else Decimal(str(value)),
        unit="kWh",
        quality=quality,
        completeness="complete",
        metadata={},
    )


# --- the energy metrics -----------------------------------------------------


def test_every_metric_reaches_the_payload_from_its_own_facts(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        day = date(2026, 7, 10)
        add(session, asset_id, mapping_id, day, "production_energy", "100")
        add(session, asset_id, mapping_id, day, "self_use_energy", "70")
        add(session, asset_id, mapping_id, day, "export_energy", "30")
        add(session, asset_id, mapping_id, day, "consumption_energy", "250")
        add(session, asset_id, mapping_id, day, "grid_import_energy", "180")
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        payload = assembled.payload

    assert payload["production_kwh"] == pytest.approx(100.0)
    assert payload["self_use_kwh"] == pytest.approx(70.0)
    assert payload["export_kwh"] == pytest.approx(30.0)
    assert payload["consumption_kwh"] == pytest.approx(250.0)
    assert payload["grid_import_kwh"] == pytest.approx(180.0)
    # Nothing that has a fact may still be declared unavailable.
    for name in ("self_use_kwh", "export_kwh", "consumption_kwh", "grid_import_kwh"):
        assert name not in payload["unavailable_fields"]


def test_a_metric_without_facts_stays_missing_while_its_neighbours_report(prepared) -> None:
    """The whole point of separate metrics: one absent signal is not four."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        day = date(2026, 7, 10)
        add(session, asset_id, mapping_id, day, "production_energy", "100")
        add(session, asset_id, mapping_id, day, "export_energy", "30")
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        payload = assembled.payload

    assert payload["production_kwh"] == pytest.approx(100.0)
    assert payload["export_kwh"] == pytest.approx(30.0)
    assert payload["consumption_kwh"] is None
    assert "consumption_kwh" in payload["unavailable_fields"]
    assert "export_kwh" not in payload["unavailable_fields"]


def test_the_dataset_digest_changes_when_a_new_metric_arrives(prepared) -> None:
    """A snapshot must not be reused after a report's inputs changed."""
    factory, (asset_id, mapping_id) = prepared
    day = date(2026, 7, 10)
    with factory() as session, session.begin():
        add(session, asset_id, mapping_id, day, "production_energy", "100")
        first = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        ).dataset.input_digest
    with factory() as session, session.begin():
        add(session, asset_id, mapping_id, day, "self_use_energy", "70")
        second = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        ).dataset.input_digest
    assert first != second


# --- tariffs and billing ----------------------------------------------------


def test_two_tariffs_may_not_price_the_same_day(prepared) -> None:
    """The database refuses the overlap V1 resolved by picking the newest row."""
    factory, (asset_id, _) = prepared
    with factory() as session, session.begin():
        session.add_all([])
        set_tariff(
            session, asset_id=asset_id, tariff_type="simple", valid_from=date(2026, 1, 1),
            valid_to=date(2027, 1, 1), prices={"simple": Decimal("0.20")}, created_by="operator",
        )
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            session.execute(
                text(
                    "INSERT INTO asset_tariffs (asset_id, tariff_type, simple_price_eur_kwh,"
                    " valid_from, valid_to, source_kind, provenance_json, created_by, created_at, updated_at)"
                    " VALUES (:a, 'simple', 0.25, '2026-06-01', '2026-12-01', 'operator', '{}', 'x', now(), now())"
                ),
                {"a": asset_id},
            )


def test_a_tariff_resolves_by_date_and_a_replacement_closes_the_one_before(prepared) -> None:
    factory, (asset_id, _) = prepared
    with factory() as session, session.begin():
        set_tariff(
            session, asset_id=asset_id, tariff_type="simple", valid_from=date(2026, 1, 1),
            prices={"simple": Decimal("0.20")}, created_by="operator",
        )
        set_tariff(
            session, asset_id=asset_id, tariff_type="simple", valid_from=date(2026, 7, 1),
            prices={"simple": Decimal("0.25")}, created_by="operator",
        )
    with factory() as session:
        june = resolve_tariff(session, asset_id=asset_id, on=date(2026, 6, 30))
        july = resolve_tariff(session, asset_id=asset_id, on=date(2026, 7, 1))
        before = resolve_tariff(session, asset_id=asset_id, on=date(2025, 12, 31))
    assert june.simple_price_eur_kwh == Decimal("0.20") and june.valid_to == date(2026, 7, 1)
    assert july.simple_price_eur_kwh == Decimal("0.25") and july.valid_to is None
    assert before is None


def test_a_cycle_tariff_must_price_the_periods_it_claims(prepared) -> None:
    """A tetra-hourly tariff with no super-vazio price is a failed import."""
    factory, (asset_id, _) = prepared
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            set_tariff(
                session, asset_id=asset_id, tariff_type="tetra-hourly", valid_from=date(2026, 1, 1),
                prices={"cheia": Decimal("0.2"), "vazio": Decimal("0.18"), "ponta": Decimal("0.24")},
                created_by="operator",
            )


def test_the_billing_configuration_reaches_the_report(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        set_billing_config(
            session, asset_id=asset_id, report_type="esco", valid_from=date(2026, 1, 1),
            solcor_price_per_kwh=Decimal("0.086"),
            default_electricity_price=Decimal("0.17828857174332818"),
            default_export_price=Decimal("0.045"), created_by="operator",
        )
        add(session, asset_id, mapping_id, date(2026, 7, 10), "production_energy", "100")
        add(session, asset_id, mapping_id, date(2026, 7, 10), "self_use_energy", "70")
        payload = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        ).payload

    assert payload["report_type"] == "esco"
    assert payload["report_type_source"] == "billing_config"
    assert payload["electricity_price"] == pytest.approx(0.17828857174332818)
    assert payload["solcor_price_per_kwh"] == pytest.approx(0.086)
    assert "electricity_price" not in payload["unavailable_fields"]
    # A price with seventeen decimals survives the round trip through Numeric.
    with factory() as session:
        stored = resolve_billing_config(session, asset_id=asset_id, on=date(2026, 7, 1))
        assert stored.default_electricity_price == Decimal("0.17828857174332818")


def test_an_esco_contract_is_not_reported_as_epc(prepared) -> None:
    """The failure this fixes: an ESCO customer receiving an EPC document."""
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        from nemsei.assets.models import Asset

        asset = session.get(Asset, asset_id)
        asset.contract_type = "ESCO"
        assert report_type_is_resolved(asset)
        add(session, asset_id, mapping_id, date(2026, 7, 10), "production_energy", "100")
        payload = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        ).payload

    assert payload["report_type"] == "esco"
    assert payload["report_type_resolved"] is True
    assert payload["report_type_source"] == "contract_attributes"


def test_an_unstated_contract_is_flagged_rather_than_silently_defaulted(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        add(session, asset_id, mapping_id, date(2026, 7, 10), "production_energy", "100")
        payload = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        ).payload

    assert payload["report_type"] == "epc"
    assert payload["report_type_resolved"] is False
    assert payload["report_type_source"] == "default"
    assert "asset.contract_type" in payload["unavailable_fields"]
    assert "report_type_defaulted_to_epc_without_contract_evidence" in payload["report_notes"]


# --- the V1 import rules ----------------------------------------------------


class FakeRow(dict):
    """A V1 row is a mapping; the importer only ever subscripts it."""

    def __getitem__(self, key):
        return self.get(key)


def test_the_metrics_come_out_of_a_real_fusionsolar_payload() -> None:
    """The exact payload shape V1 stored, with its own numbers."""
    row = FakeRow(
        payload_json=(
            '{"collectTime": 1735689600000, "stationCode": "NE=139401017", "dataItemMap":'
            ' {"PVYield": 809.17, "selfUsePower": 803.17, "ongrid_power": 6.0,'
            ' "use_power": 3173.17, "buyPower": 2370.0}}'
        )
    )
    values = metrics_from_row(row)
    assert values["production_energy"] == Decimal("809.17")
    assert values["self_use_energy"] == Decimal("803.17")
    assert values["export_energy"] == Decimal("6.0")
    assert values["consumption_energy"] == Decimal("3173.17")
    assert values["grid_import_energy"] == Decimal("2370.0")
    # The identity holds, so nothing is rejected.
    assert identity_violations(values) == []


def test_a_row_that_exports_more_than_it_produces_is_rejected() -> None:
    """A real V1 station does this. Exporting what was never generated is impossible."""
    values = {
        "production_energy": Decimal("0.2"),
        "export_energy": Decimal("107.48"),
        "self_use_energy": Decimal("0"),
        "consumption_energy": Decimal("0"),
        "grid_import_energy": None,
    }
    assert identity_violations(values) == ["export_energy"]


def test_a_battery_plant_is_not_rejected_for_failing_the_production_identity() -> None:
    """Sigenergy: production exceeds self-use plus export because a battery charged.

    Deriving one metric from another would have invented a number here, which is
    why the identity only ever rejects the impossible.
    """
    values = {
        "production_energy": Decimal("827.73"),
        "self_use_energy": Decimal("630.38"),
        "export_energy": Decimal("195.30"),
        "consumption_energy": Decimal("1097.02"),
        "grid_import_energy": Decimal("466.64"),
    }
    assert identity_violations(values) == []


def test_a_sigenergy_row_reads_its_columns_when_there_is_no_payload() -> None:
    row = FakeRow(
        payload_json=None, production_kwh=827.73, self_use_kwh=630.38,
        export_kwh=195.3, consumption_kwh=1097.02, grid_import_kwh=466.64,
    )
    values = metrics_from_row(row)
    assert values["production_energy"] == Decimal("827.73")
    assert values["grid_import_energy"] == Decimal("466.64")


def test_a_blank_reading_is_missing_and_never_zero() -> None:
    row = FakeRow(
        payload_json='{"dataItemMap": {"PVYield": 10.0}}',
        production_kwh=None, self_use_kwh="", export_kwh="  ",
        consumption_kwh="n/d", grid_import_kwh=None,
    )
    values = metrics_from_row(row)
    assert values["production_energy"] == Decimal("10.0")
    for metric in ("self_use_energy", "export_energy", "consumption_energy", "grid_import_energy"):
        assert values[metric] is None


# --- end to end -------------------------------------------------------------


def test_a_complete_report_renders_from_persisted_inputs_alone(prepared) -> None:
    factory, (asset_id, mapping_id) = prepared
    with factory() as session, session.begin():
        from nemsei.assets.models import Asset

        session.get(Asset, asset_id).contract_type = "ESCO"
        set_billing_config(
            session, asset_id=asset_id, report_type="esco", valid_from=date(2026, 1, 1),
            solcor_price_per_kwh=Decimal("0.086"),
            default_electricity_price=Decimal("0.178"),
            default_export_price=Decimal("0.045"), created_by="operator",
        )
        set_tariff(
            session, asset_id=asset_id, tariff_type="tetra-hourly", cycle_type="weekly",
            valid_from=date(2026, 1, 1),
            prices={
                "ponta": Decimal("0.24615"), "cheia": Decimal("0.20103"),
                "vazio": Decimal("0.18137"), "super_vazio": Decimal("0.17025"),
            },
            source_kind="v1_import", created_by="operator",
        )
        for day, values in (
            (date(2026, 7, 10), ("100", "70", "30", "250", "180")),
            (date(2026, 7, 11), ("120", "80", "40", "260", "180")),
        ):
            for metric, value in zip(
                ("production_energy", "self_use_energy", "export_energy",
                 "consumption_energy", "grid_import_energy"),
                values, strict=True,
            ):
                add(session, asset_id, mapping_id, day, metric, value)
        assembled = assemble_asset_report(
            session, asset_id=asset_id, period=monthly_period("2026-07"), built_by="operator"
        )
        payload = assembled.payload

    assert payload["production_kwh"] == pytest.approx(220.0)
    assert payload["self_use_kwh"] == pytest.approx(150.0)
    assert payload["export_kwh"] == pytest.approx(70.0)
    assert payload["report_type"] == "esco"
    assert payload["tariff_type"] == "tetra-hourly"
    assert len(payload["tariff_period_breakdown"]) == 4
    # Savings are now a real figure rather than zero from absent inputs.
    assert payload["savings_eur"] > 0
    assert payload["export_revenue_eur"] > 0
    # Only what genuinely has no source anywhere is still declared absent.
    assert set(payload["unavailable_fields"]) == {
        "self_use_cheia_kwh", "self_use_ponta_kwh", "self_use_vazio_kwh",
        "self_use_super_vazio_kwh", "availability_pct",
    }

    pdf = build_customer_report_pdf(payload)
    assert pdf[:5] == b"%PDF-"
    workbook = build_asset_report_workbook(excel_payload_from_report(payload))
    energy = workbook["Energia"]
    values = {energy.cell(row, 1).value: energy.cell(row, 2).value for row in range(3, 8)}
    assert values["production_kwh"] == "220.00"
    assert values["self_use_kwh"] == "150.00"
    assert values["consumption_kwh"] == "510.00"


def test_a_replacement_may_not_start_before_the_row_it_replaces(prepared) -> None:
    """Re-running an import must not shorten a window into nothing.

    Closing a row on a day it already covers from would give it a zero-length
    window. That is two statements about the same dates, and guessing which is
    right is guessing what a customer is charged.
    """
    factory, (asset_id, _) = prepared
    with factory() as session, session.begin():
        set_tariff(
            session, asset_id=asset_id, tariff_type="simple", valid_from=date(2026, 1, 1),
            valid_to=date(2027, 1, 1), prices={"simple": Decimal("0.20")}, created_by="operator",
        )
    with pytest.raises(ValueError, match="supersede or delete it explicitly"):
        with factory() as session, session.begin():
            set_tariff(
                session, asset_id=asset_id, tariff_type="simple", valid_from=date(2026, 1, 1),
                valid_to=date(2027, 1, 1), prices={"simple": Decimal("0.25")}, created_by="operator",
            )

