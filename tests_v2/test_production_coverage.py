"""A aplicação tem de saber explicar porque é que uma central não tem produção.

Cada teste põe uma central numa condição concreta e verifica que o
diagnóstico devolve *essa* condição -- não uma genérica, e não a primeira
de uma lista de tudo o que também é verdade. É por isso que a ordem em que
a cadeia é percorrida é testada explicitamente: uma central sem mapping
nenhum também não tem política de fonte, e dizer "sem política de fonte"
manda o operador ao ecrã errado.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.production_coverage import (
    COVERAGE_STATES,
    RECENT_FACT_TOLERANCE_DAYS,
    STATE_AMBIGUOUS_SOURCE_POLICY,
    STATE_CONNECTION_DISABLED,
    STATE_CURSOR_MISSING,
    STATE_CURSOR_STALE,
    STATE_MAPPING_INACTIVE,
    STATE_NOT_INITIALIZED,
    STATE_NOT_SCHEDULED,
    STATE_NO_PROVIDER_MAPPING,
    STATE_NO_RECENT_FACT,
    STATE_NO_SOURCE_POLICY,
    STATE_OK,
    STATE_PRODUCTION_CONTRACT_MISSING,
    STATE_RATE_LIMITED,
    STATE_SYNC_FAILED,
    assess_production_coverage,
    coverage_summary,
)
from nemsei.jobs.models import ScheduleState
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.registry import ProviderCapability
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import SyncRun
from tests_v2.production_scheduling_fixtures import seed_production_cursor
from tests_v2.test_migrations import upgrade


TODAY = date(2026, 9, 7)
YESTERDAY = TODAY - timedelta(days=1)


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def contract_environment(monkeypatch, reference="dev"):
    """The operator-verified production contract for a credential reference."""
    prefix = f"NEMSEI_V2_FUSIONSOLAR_{reference.upper()}"
    monkeypatch.setenv(f"{prefix}_PRODUCTION_TIMEZONE", "Europe/Lisbon")
    monkeypatch.setenv(f"{prefix}_PRODUCTION_UNIT", "kWh")


def _connection(session, *, key="conta", enabled=True, configured=True, credential="dev", initial_from=None):
    return create_connection(
        session, provider_code="fusionsolar", connection_key=key, display_name=f"Conta {key}",
        credential_reference=credential, enabled=enabled,
        configuration_status="configured" if configured else "not_configured",
    )


def _mapped_asset(session, connection, *, name="Central", with_policy=True, mapping_status="active"):
    asset = create_asset(session, canonical_name=name)
    mapping = create_mapping(
        session, asset_id=asset.id, provider_connection_id=connection.id,
        external_id=f"ST-{asset.id}", valid_from=date(2020, 1, 1),
    )
    if mapping_status != "active":
        mapping.mapping_status = mapping_status
    if with_policy:
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=mapping.id,
            source_use="production", priority=1, valid_from=date(2020, 1, 1),
        )
    session.flush()
    return asset, mapping


def _schedule(session, connection_id, *, kind="incremental"):
    session.add(
        ScheduleState(
            schedule_key=f"production.{kind}:{connection_id}",
            next_run_at=utc_now(),
            updated_at=utc_now(),
        )
    )
    session.flush()


def _sync_run(session, connection_id, *, status, error_code=None, started_at=None):
    run = SyncRun(
        provider_connection_id=connection_id,
        capability=ProviderCapability.PRODUCTION_HISTORY.value,
        status=status,
        started_at=started_at or utc_now(),
        finished_at=started_at or utc_now(),
        completeness="complete" if status == "success" else "none",
        error_code=error_code,
        metadata_json={},
    )
    session.add(run)
    session.flush()
    return run


def _fact(session, *, asset_id, mapping_id, on: date, value="120.5"):
    record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"fusionsolar-day:{on.isoformat()}",
        metric_kind="production_energy",
        period_start=datetime.combine(on, datetime.min.time(), tzinfo=timezone.utc),
        period_end=datetime.combine(on + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
        granularity="day",
        value=Decimal(value),
        unit="kWh",
        quality="complete",
        completeness="complete",
    )


def _state(session, **kwargs):
    return assess_production_coverage(session, on=TODAY, **kwargs)[0].state


def _finding(session, **kwargs):
    return assess_production_coverage(session, on=TODAY, **kwargs)[0]


# ---------------------------------------------------------------------------
# A cadeia, elo a elo.
# ---------------------------------------------------------------------------


def test_an_asset_with_no_mapping_at_all(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        create_asset(session, canonical_name="Sem mapping")
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_NO_PROVIDER_MAPPING
    assert "mappings" in finding.recommended_action


def test_a_mapping_that_exists_but_is_not_active(settings, monkeypatch):
    """Distinto de "sem mapping": há uma candidatura, falta aprová-la."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection, with_policy=False, mapping_status="pending_review")
    with factory() as session:
        assert _state(session) == STATE_MAPPING_INACTIVE


def test_an_active_mapping_with_no_production_source_policy(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection, with_policy=False)
    with factory() as session:
        assert _state(session) == STATE_NO_SOURCE_POLICY


def test_two_primary_policies_at_the_same_priority_are_ambiguous(settings, monkeypatch):
    """A mesma pergunta que `resolve_source_policy` faz -- mas reportada em
    vez de levantada, porque uma exceção não desenha uma linha de tabela."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        asset, _mapping = _mapped_asset(session, connection)
        second = create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id,
            external_id="ST-ALT", valid_from=date(2020, 1, 1),
        )
        create_source_policy(
            session, asset_id=asset.id, provider_mapping_id=second.id,
            source_use="production", priority=1, valid_from=date(2020, 1, 1),
        )
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_AMBIGUOUS_SOURCE_POLICY
    assert "2 políticas" in finding.recommended_action


def test_a_connection_disabled_after_its_mappings_were_approved(settings, monkeypatch):
    """A ordem real: a ligação esteve activa, os mappings foram aprovados, e
    depois alguém desligou-a. Um mapping activo não pode sequer ser criado
    numa ligação desactivada (`providers.service.create_mapping`), por isso
    esta é a única forma de a frota chegar a este estado."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        connection.enabled = False
        connection.configuration_status = "disabled"
    with factory() as session:
        assert _state(session) == STATE_CONNECTION_DISABLED


def test_a_connection_without_the_verified_production_contract(settings, monkeypatch):
    """Faltam `<PREFIX>_PRODUCTION_TIMEZONE` / `_PRODUCTION_UNIT=kWh`."""
    factory = factory_for(settings, monkeypatch)
    monkeypatch.delenv("NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_TIMEZONE", raising=False)
    monkeypatch.delenv("NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_UNIT", raising=False)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
    with factory() as session:
        assert _state(session) == STATE_PRODUCTION_CONTRACT_MISSING


def test_a_connection_with_no_cursor_and_no_initial_date_is_not_initialized(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_NOT_INITIALIZED
    assert finding.cursor_last_completed_day is None


def test_a_connection_with_an_initial_date_but_no_cursor_yet(settings, monkeypatch):
    """O bootstrap está por correr -- outra condição, outra acção."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        connection.initial_production_from_date = date(2026, 8, 1)
        _mapped_asset(session, connection)
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_CURSOR_MISSING
    assert "2026-08-01" in finding.recommended_action


def test_a_connection_with_a_cursor_but_no_schedule(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_NOT_SCHEDULED
    assert finding.scheduled is False


def test_a_rate_limited_connection_is_not_reported_as_a_failure(settings, monkeypatch):
    """Uma conta partilhada a recusar é uma condição de operação conhecida,
    não uma avaria -- e a acção recomendada diz que se recupera sozinha."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="rate_limited", error_code="rate_limited")
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_RATE_LIMITED
    assert finding.last_sync_status == "rate_limited"


def test_a_stale_cursor_names_the_condition_and_the_fix(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=date(2026, 6, 1))
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="failed", error_code="configuration")
    with factory() as session:
        finding = _finding(session)
    # A falha da corrida é consequência do cursor atrasado, não a causa:
    # o intervalo é que excede o que um incremental pode pedir.
    assert finding.state == STATE_CURSOR_STALE
    assert "backfill" in finding.recommended_action


def test_a_failed_sync_with_a_healthy_cursor(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="failed", error_code="authentication")
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_SYNC_FAILED
    assert finding.last_sync_error_code == "authentication"


def test_a_healthy_connection_whose_plant_receives_nothing(settings, monkeypatch):
    """Tudo verde e mesmo assim vazia: o código de estação já não existe na
    conta, ou o mapping aponta para outra coisa. É uma condição real e não
    tem nada que ver com a saúde da ligação."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="success")
    with factory() as session:
        assert _state(session) == STATE_NO_RECENT_FACT


def test_an_installation_with_recent_production_is_ok(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        asset, mapping = _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="success")
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=YESTERDAY)
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_OK
    assert finding.last_production_day == YESTERDAY


def test_a_short_gap_is_tolerated_and_a_longer_one_is_not(settings, monkeypatch):
    """A sincronização corre uma vez por dia contra uma conta limitada e cobre
    D-1, por isso um dia em falta é um adiamento normal. Chamar-lhe avaria
    fazia este ecrã gritar todas as manhãs."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    last_fact_day = TODAY - timedelta(days=RECENT_FACT_TOLERANCE_DAYS)
    with factory() as session, session.begin():
        connection = _connection(session)
        asset, mapping = _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="success")
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=last_fact_day)

    with factory() as session:
        # Exactamente na tolerância: ainda coberta.
        assert assess_production_coverage(session, on=TODAY)[0].state == STATE_OK
        # Um dia para lá dela: deixa de contar.
        assert assess_production_coverage(session, on=TODAY + timedelta(days=1))[0].state == STATE_NO_RECENT_FACT


def test_a_superseded_fact_does_not_keep_a_plant_looking_covered(settings, monkeypatch):
    """`production_facts` é append-only: uma correção para "sem valor" não
    apaga a linha anterior, e somar/olhar as linhas cruas faria a central
    parecer coberta por um valor que já foi substituído."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        asset, mapping = _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="success")
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=YESTERDAY, value="120.5")
        asset_id, mapping_id = asset.id, mapping.id

    with factory() as session, session.begin():
        record_production_fact(
            session,
            asset_id=asset_id,
            provider_mapping_id=mapping_id,
            source_fact_key=f"fusionsolar-day:{YESTERDAY.isoformat()}",
            metric_kind="production_energy",
            period_start=datetime.combine(YESTERDAY, datetime.min.time(), tzinfo=timezone.utc),
            period_end=datetime.combine(TODAY, datetime.min.time(), tzinfo=timezone.utc),
            granularity="day",
            value=None,
            unit="kWh",
            quality="missing",
            completeness="partial",
        )

    with factory() as session:
        assert _state(session) == STATE_NO_RECENT_FACT


# ---------------------------------------------------------------------------
# Frota inteira: contagens, e o custo em queries.
# ---------------------------------------------------------------------------


def test_the_summary_counts_only_installations_actually_receiving_production(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        healthy, mapping = _mapped_asset(session, connection, name="Boa")
        _mapped_asset(session, connection, name="Sem política", with_policy=False)
        create_asset(session, canonical_name="Sem mapping")
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="success")
        _fact(session, asset_id=healthy.id, mapping_id=mapping.id, on=YESTERDAY)

    with factory() as session:
        findings = assess_production_coverage(session, on=TODAY)
    summary = coverage_summary(findings)
    assert summary["total"] == 3
    assert summary["with_recent_production"] == 1
    assert summary["with_problem"] == 2
    assert summary["by_state"][STATE_NO_SOURCE_POLICY] == 1
    assert summary["by_state"][STATE_NO_PROVIDER_MAPPING] == 1


def test_the_fleet_sweep_does_not_grow_queries_with_the_fleet(settings, monkeypatch):
    """Nada de N+1: o custo de 3 centrais e o de 12 é o mesmo número de
    queries. É a propriedade que torna a página abrível quando a frota já
    está a arder, que é exactamente quando alguém a abre."""
    from sqlalchemy import event

    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)

    def build(count: int) -> list[int]:
        with factory() as session, session.begin():
            connection = _connection(session, key=f"conta-{count}")
            ids = []
            for number in range(count):
                asset, mapping = _mapped_asset(session, connection, name=f"Central {count}-{number}")
                _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=YESTERDAY)
                ids.append(asset.id)
            seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
            _schedule(session, connection.id)
            _sync_run(session, connection.id, status="success")
            return ids

    def count_queries(asset_ids: list[int]) -> int:
        with factory() as session:
            statements: list[str] = []

            def record(_conn, _cursor, statement, *_args):
                statements.append(statement)

            bind = session.get_bind()
            event.listen(bind, "before_cursor_execute", record)
            try:
                assess_production_coverage(session, asset_ids=asset_ids, on=TODAY)
            finally:
                event.remove(bind, "before_cursor_execute", record)
            return len(statements)

    small = count_queries(build(3))
    large = count_queries(build(12))
    assert small == large


def test_every_state_the_module_can_return_is_in_its_own_vocabulary():
    """Guarda contra um estado novo que chegue à interface como código cru."""
    import nemsei.diagnostics.production_coverage as module

    declared = {value for name, value in vars(module).items() if name.startswith("STATE_")}
    assert declared == set(COVERAGE_STATES)


def test_the_diagnosis_never_reads_a_secret_back_out():
    """Estrutural: o módulo pode perguntar se o contrato está configurado,
    nunca devolver o que lá está. E não importa nenhum cliente de provider,
    por isso não pode gastar orçamento de chamadas a explicar porque é que
    as chamadas falham."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "nemsei" / "diagnostics" / "production_coverage.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [name for name in imported if name.startswith("nemsei.integrations")]

    # `os.environ.get(...)` aparece só dentro do predicado booleano, e o seu
    # valor nunca sai da função.
    from nemsei.diagnostics.production_coverage import ProductionCoverage

    fields = set(ProductionCoverage.__dataclass_fields__)
    assert not {field for field in fields if "credential" in field or "password" in field or "secret" in field}
