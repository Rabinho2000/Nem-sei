from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def device_revision() -> str:
    """Locate the device migration without pinning a revision name."""
    for revision in ScriptDirectory.from_config(Config("alembic.ini")).walk_revisions():
        if "canonical_devices" in revision.path:
            return revision.revision
    raise AssertionError("The canonical device migration is missing from the graph.")


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def test_initial_migration_creates_foundation_tables(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    assert {
        "jobs", "job_events", "scheduler_leases", "schedule_state",
        "organizations", "assets", "asset_aliases", "devices", "provider_connections",
        "asset_provider_mappings", "legacy_import_runs", "legacy_import_records",
        "integration_health", "sync_runs", "sync_cursors", "provider_request_states",
        "provider_request_attempts", "asset_source_policies", "monitoring_observations", "monitoring_current_states", "production_facts",
        "operator_audit_events", "legacy_identity_decisions",
        "alembic_version",
    } <= set(inspect(engine).get_table_names())


def test_device_migration_downgrades_cleanly_when_no_devices_exist(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    assert "devices" in set(inspect(engine).get_table_names())
    command.downgrade(Config("alembic.ini"), f"{device_revision()}-1")
    remaining = set(inspect(create_engine(settings.database_url)).get_table_names())
    assert "devices" not in remaining
    assert {"assets", "asset_provider_mappings", "legacy_import_records"} <= remaining
    command.upgrade(Config("alembic.ini"), "head")
    assert "devices" in set(inspect(create_engine(settings.database_url)).get_table_names())


def test_device_migration_refuses_to_discard_existing_devices(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO assets (public_id, canonical_name, normalized_name, lifecycle_status, review_status, timezone_source, created_at, updated_at) "
                "VALUES ('asset-public-1', 'Alpha Solar', 'alpha solar', 'unknown', 'clear', 'manual', now(), now())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO devices (public_id, asset_id, device_kind, lifecycle_status, review_status, valid_from, created_at, updated_at) "
                "SELECT 'device-public-1', id, 'inverter', 'active', 'clear', CURRENT_DATE, now(), now() FROM assets LIMIT 1"
            )
        )
    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(Config("alembic.ini"), f"{device_revision()}-1")
    assert "devices" in set(inspect(create_engine(settings.database_url)).get_table_names())


def test_portfolio_report_run_migration_refuses_to_discard_a_run(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    from datetime import date, datetime, timezone
    from decimal import Decimal

    from nemsei.assets.service import create_asset
    from nemsei.db.session import build_session_factory
    from nemsei.monitoring.service import record_production_fact
    from nemsei.portfolios.reporting import generate_report_run
    from nemsei.portfolios.service import add_member, create_portfolio
    from nemsei.providers.service import create_connection, create_mapping

    factory = build_session_factory(create_engine(settings.database_url))
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="P", created_by="op")
        asset = create_asset(session, canonical_name="A")
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="c1", display_name="c1",
            credential_reference="REF", enabled=True, configuration_status="configured",
        )
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="E1")
        record_production_fact(
            session, asset_id=asset.id, provider_mapping_id=mapping.id, source_fact_key="k",
            period_start=datetime(2026, 3, 10, tzinfo=timezone.utc), period_end=datetime(2026, 3, 11, tzinfo=timezone.utc),
            granularity="day", metric_kind="production_energy", value=Decimal("10"), unit="kWh",
            quality="complete", completeness="complete", metadata={},
        )
        add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="op")
        generate_report_run(session, portfolio_id=portfolio.id, report_month="2026-03", actor="op")

    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(Config("alembic.ini"), "0013_portfolios")
    assert "portfolio_report_runs" in set(inspect(create_engine(settings.database_url)).get_table_names())


def test_availability_source_kind_migration_labels_existing_rows_without_losing_them(settings, monkeypatch) -> None:
    """0041 over a *populated* 0040 database, not an empty one.

    The realistic upgrade path for this schema: production already holds
    `fusionsolar_sampled` availability rows written before the
    contractual/operational split existed. They must come out the other side
    intact and labelled `operational` -- the only honest label for a port of
    V1's sampled engine -- and a downgrade must not delete them.
    """
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "0040_reporting_availability")

    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO assets (public_id, canonical_name, normalized_name, lifecycle_status, review_status, timezone_source, created_at, updated_at) "
                "VALUES ('asset-avail-0041', 'Availability Plant', 'availability plant', 'unknown', 'clear', 'manual', now(), now())"
            )
        )
        asset_id = connection.execute(text("SELECT id FROM assets WHERE public_id = 'asset-avail-0041'")).scalar_one()
        connection.execute(
            text(
                "INSERT INTO devices (asset_id, public_id, device_kind, lifecycle_status, review_status, valid_from, created_at, updated_at) "
                "VALUES (:asset_id, 'device-avail-0041', 'inverter', 'active', 'clear', DATE '2026-01-01', now(), now())"
            ),
            {"asset_id": asset_id},
        )
        device_id = connection.execute(text("SELECT id FROM devices WHERE public_id = 'device-avail-0041'")).scalar_one()
        connection.execute(
            text(
                "INSERT INTO device_availability_daily (device_id, asset_id, availability_date, availability_pct, "
                "valid_sample_count, minimum_required_samples, coverage_status, warning_codes_json, source, "
                "calculated_at, created_at, updated_at) "
                "VALUES (:device_id, :asset_id, DATE '2026-09-01', 97.50, 12, 5, 'complete', '[]', "
                "'fusionsolar_sampled', now(), now(), now())"
            ),
            {"device_id": device_id, "asset_id": asset_id},
        )
        connection.execute(
            text(
                "INSERT INTO asset_availability_daily (asset_id, availability_date, availability_pct, valid_sample_count, "
                "expected_device_count, observed_device_count, minimum_required_samples, coverage_status, "
                "warning_codes_json, source, calculation_details_json, calculated_at, created_at, updated_at) "
                "VALUES (:asset_id, DATE '2026-09-01', 97.50, 12, 1, 1, 5, 'complete', '[]', 'fusionsolar_sampled', "
                "'{}', now(), now(), now())"
            ),
            {"asset_id": asset_id},
        )

    command.upgrade(config, "head")
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT availability_pct, source, source_kind FROM asset_availability_daily")
        ).all()
        assert rows == [(pytest.approx(97.50), "fusionsolar_sampled", "operational")]
        device_rows = connection.execute(text("SELECT source, source_kind FROM device_availability_daily")).all()
        assert device_rows == [("fusionsolar_sampled", "operational")]

        # The pair constraint is structural, not a convention: the database
        # itself refuses to store the sampled engine's output as contractual.
        with pytest.raises(Exception):
            connection.execute(
                text("UPDATE asset_availability_daily SET source_kind = 'contractual'")
            )

    # Downgrade drops what 0041 added and keeps every row.
    command.downgrade(config, "0040_reporting_availability")
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM asset_availability_daily")).scalar() == 1
        assert connection.execute(text("SELECT count(*) FROM device_availability_daily")).scalar() == 1
        assert "source_kind" not in {column["name"] for column in inspect(engine).get_columns("asset_availability_daily")}

    command.upgrade(config, "head")
    assert "source_kind" in {
        column["name"] for column in inspect(create_engine(settings.database_url)).get_columns("asset_availability_daily")
    }


def test_production_scheduling_migration_round_trips_over_a_populated_table(settings, monkeypatch) -> None:
    """0044 corre sobre `provider_connections` com linhas, e volta atrás.

    As três colunas são todas anuláveis ou com default, por isso nenhuma
    linha existente pode violá-las e nada é reescrito -- e o downgrade não
    perde nada derivado: os cursores, os jobs e os factos vivem noutras
    tabelas.
    """
    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO provider_connections (provider_code, connection_key, display_name, enabled, "
                "configuration_status, created_at, updated_at) "
                "VALUES ('fusionsolar', 'legacy-conn', 'Conta antiga', true, 'configured', now(), now())"
            )
        )
    columns = {column["name"] for column in inspect(engine).get_columns("provider_connections")}
    assert {"production_sync_enabled", "production_sync_interval_hours", "initial_production_from_date"} <= columns
    with engine.begin() as connection:
        # O default é o conservador: ligar uma connection nunca começa a
        # chamar um provider.
        assert connection.execute(text("SELECT production_sync_enabled FROM provider_connections")).scalar() is False

    command.downgrade(Config("alembic.ini"), "0043_device_history_facts")
    after = inspect(create_engine(settings.database_url))
    assert "production_sync_enabled" not in {column["name"] for column in after.get_columns("provider_connections")}
    assert after.get_columns("provider_connections")  # a tabela e as suas linhas ficam
    command.upgrade(Config("alembic.ini"), "head")
    assert "production_sync_enabled" in {
        column["name"] for column in inspect(create_engine(settings.database_url)).get_columns("provider_connections")
    }


def test_the_production_interval_column_refuses_a_non_positive_cadence(settings, monkeypatch) -> None:
    """Um intervalo de zero horas seria um agendamento a disparar em ciclo
    contra uma conta limitada; a base recusa-o antes de a config o ver."""
    from sqlalchemy.exc import IntegrityError

    upgrade(settings, monkeypatch)
    engine = create_engine(settings.database_url)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO provider_connections (provider_code, connection_key, display_name, enabled, "
                    "configuration_status, production_sync_interval_hours, created_at, updated_at) "
                    "VALUES ('fusionsolar', 'bad-interval', 'Conta', true, 'configured', 0, now(), now())"
                )
            )
