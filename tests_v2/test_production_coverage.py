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
    STATE_CREDENTIAL_REFERENCE_MISSING,
    STATE_SYNC_DEFERRED,
    STATE_SYNC_FAILED,
    STATE_UNKNOWN,
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


def test_a_connection_with_no_credential_reference(settings, monkeypatch):
    """Reportado antes do contrato de ambiente, porque é a referência que
    dá o prefixo das variáveis -- sem ela não há sequer onde as procurar."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        connection.credential_reference = None
    with factory() as session:
        assert _state(session) == STATE_CREDENTIAL_REFERENCE_MISSING


def test_a_deferred_sync_is_a_cooldown_not_a_fault(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=YESTERDAY)
        _schedule(session, connection.id)
        _sync_run(session, connection.id, status="deferred")
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_SYNC_DEFERRED
    assert "esperar" in finding.recommended_action


def test_a_primary_policy_pointing_at_a_mapping_that_is_no_longer_active(settings, monkeypatch):
    """O estado por apurar, e a única coisa que o produz.

    A central foi remapeada: o mapping novo está activo, mas a política de
    produção ficou a apontar para o antigo, que já expirou. Não é "sem
    mapping" (há um activo) nem "sem política" (há uma), e é por isso que
    tem estado próprio: a acção é ir corrigir a política, não criar nada.

    Se o mapping antigo fosse o único, o diagnóstico correcto passaria a
    ser `no_provider_mapping` -- e é, o que este arranjo confirma por
    contraste.
    """
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session)
        asset, old_mapping = _mapped_asset(session, connection)
        old_mapping.valid_to = date(2020, 6, 1)
        create_mapping(
            session, asset_id=asset.id, provider_connection_id=connection.id,
            external_id="ST-NOVO", valid_from=date(2020, 6, 2),
        )
    with factory() as session:
        finding = _finding(session)
    assert finding.state == STATE_UNKNOWN
    assert "source-policies" in finding.recommended_action


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


# ---------------------------------------------------------------------------
# Fontes push: sem cursor, sem bootstrap, sem agendamento.
#
# A cadeia inteira entre o contrato e a recência existe para uma série que é
# *ida buscar*. O dongle Huawei SCADA não tem nenhuma dessas coisas: liga-se
# sozinho e `integrations/huawei_scada/rollup.py` integra amostras que o V2 já
# tem, sem uma única chamada ao provider. Perguntar-lhe pelo cursor e depois
# recomendar um "primeiro backfill" nomeia uma correção que não existe.
#
# O discriminador é o `implemented_capabilities` do registry, onde
# PRODUCTION_HISTORY está deliberadamente ausente para este provider e o
# comentário diz porquê. Não é o nome da ligação.
# ---------------------------------------------------------------------------


def _push_connection(session, *, key="dongle", credential="primary"):
    return create_connection(
        session, provider_code="huawei_scada", connection_key=key,
        display_name=f"SCADA {key}", credential_reference=credential,
        enabled=True, configuration_status="configured",
    )


def test_a_push_source_with_recent_facts_is_ok_without_any_cursor(settings, monkeypatch):
    """Caso D. Sem cursor, sem data inicial, sem agendamento — e na mesma OK."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _push_connection(session)
        asset, mapping = _mapped_asset(session, connection)
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=TODAY - timedelta(days=1))
        finding = _finding(session)

    assert finding.state == STATE_OK
    assert finding.production_ingestion == "push"
    # O que esta correção existe para impedir.
    assert finding.state != STATE_NOT_INITIALIZED
    assert "backfill" not in finding.recommended_action.lower()


def test_a_push_source_whose_readings_stopped_is_a_recency_problem(settings, monkeypatch):
    """Caso E. O equipamento deixou de chegar — não é falta de inicialização."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _push_connection(session)
        asset, mapping = _mapped_asset(session, connection)
        _fact(
            session, asset_id=asset.id, mapping_id=mapping.id,
            on=TODAY - timedelta(days=RECENT_FACT_TOLERANCE_DAYS + 5),
        )
        finding = _finding(session)

    assert finding.state == STATE_NO_RECENT_FACT
    assert finding.production_ingestion == "push"
    assert "backfill" not in finding.recommended_action.lower()


def test_a_push_source_that_never_delivered_a_fact_is_still_not_uninitialised(settings, monkeypatch):
    """Caso F. Ausência total de facto continua a ser uma questão de recência."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _push_connection(session)
        _mapped_asset(session, connection)
        finding = _finding(session)

    assert finding.state == STATE_NO_RECENT_FACT
    assert finding.last_production_day is None
    assert finding.state != STATE_NOT_INITIALIZED


def test_a_push_source_is_never_reported_as_unscheduled(settings, monkeypatch):
    """Não há `production.*` schedule para uma fonte push, e isso não é falha."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = _push_connection(session)
        asset, mapping = _mapped_asset(session, connection)
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=TODAY - timedelta(days=1))
        finding = _finding(session)

    assert finding.scheduled is False
    assert finding.state == STATE_OK


def test_a_polling_source_still_requires_its_cursor_and_bootstrap(settings, monkeypatch):
    """Caso A, ao lado do D: o alívio é só para push, não para toda a gente."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session:
        connection = _connection(session)
        asset, mapping = _mapped_asset(session, connection)
        # Facto fresco, e mesmo assim não inicializada: a fonte é de polling e
        # o cursor continua a ser a próxima coisa a corrigir.
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=TODAY - timedelta(days=1))
        finding = _finding(session)

    assert finding.state == STATE_NOT_INITIALIZED
    assert finding.production_ingestion == "polling"


def test_a_polling_source_with_cursor_schedule_and_a_recent_fact_is_ok(settings, monkeypatch):
    """Caso B, explícito ao lado do C que já existia."""
    factory = factory_for(settings, monkeypatch)
    contract_environment(monkeypatch)
    with factory() as session:
        connection = _connection(session)
        asset, mapping = _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=TODAY - timedelta(days=1))
        _schedule(session, connection.id)
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=TODAY - timedelta(days=1))
        finding = _finding(session)

    assert finding.state == STATE_OK
    assert finding.production_ingestion == "polling"


def test_the_push_discriminator_is_the_registry_not_the_connection_name(settings, monkeypatch):
    """Estrutural: nada aqui pode passar a decidir por nome.

    Um `if "scada" in name.lower()` classificaria à mesma os casos acima, por
    isso os testes de comportamento não chegam para fixar *como* a decisão é
    tomada. Esta é a diferença entre um facto do registry e uma string.
    """
    import ast
    from pathlib import Path

    from nemsei.diagnostics.production_coverage import _polls_production_history
    from nemsei.providers.registry import ProviderCapability, ProviderCode, descriptor_for

    # O registry é a fonte, e continua a dizer o que este módulo assume.
    assert ProviderCapability.PRODUCTION_HISTORY not in descriptor_for(
        ProviderCode.HUAWEI_SCADA
    ).implemented_capabilities
    assert ProviderCapability.PRODUCTION_HISTORY in descriptor_for(
        ProviderCode.FUSIONSOLAR
    ).implemented_capabilities

    # Uma ligação com nome enganador é classificada pela capacidade, não pelo nome.
    class _Fake:
        provider_code = ProviderCode.FUSIONSOLAR.value
        display_name = "SCADA dongle push"

    assert _polls_production_history(_Fake()) is True

    # E o módulo não olha para nomes para decidir isto.
    source = Path(__file__).resolve().parents[1] / "src" / "nemsei" / "diagnostics" / "production_coverage.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_polls_production_history":
            names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert "display_name" not in names
            assert "connection_key" not in names
            break
    else:  # pragma: no cover - a função tem de existir
        raise AssertionError("_polls_production_history desapareceu")


def test_an_unknown_provider_keeps_the_stricter_polling_chain(settings, monkeypatch):
    """Um provider que este módulo não conhece não é prova de que nada é ido buscar."""
    from nemsei.diagnostics.production_coverage import _polls_production_history

    class _Unknown:
        provider_code = "um_provider_que_nao_existe"

    assert _polls_production_history(_Unknown()) is True


# ---------------------------------------------------------------------------
# O contrato de produção é um facto do *deployment*, não do processo que pergunta.
# ---------------------------------------------------------------------------


def test_the_contract_check_reads_the_environment_it_is_given(settings, monkeypatch):
    """Encontrado em produção, 2026-09-07.

    O classificador lia `os.environ` do processo. A página corre no `web`, que
    não carregava as duas variáveis que o `worker` e o `scheduler` carregam, e
    134 de 267 instalações apareciam como `production_contract_missing` com o
    contrato configurado -- a causa real era um cursor encravado. O `env` é
    explícito para que a dependência possa ser afirmada por um teste em vez de
    depender de onde o código calhou correr.
    """
    factory = factory_for(settings, monkeypatch)
    # O ambiente do processo não tem o contrato, de propósito.
    monkeypatch.delenv("NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_TIMEZONE", raising=False)
    monkeypatch.delenv("NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_UNIT", raising=False)
    with factory() as session:
        connection = _connection(session)
        asset, mapping = _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=TODAY - timedelta(days=1))
        _schedule(session, connection.id)
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=TODAY - timedelta(days=1))

        # Sem contrato visível: o diagnóstico é o do contrato em falta.
        assert _state(session) == STATE_PRODUCTION_CONTRACT_MISSING

        # Com o mesmo contrato entregue explicitamente: o veredicto muda, sem
        # nada ter mudado na base de dados nem no ambiente do processo.
        supplied = {
            "NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_TIMEZONE": "Europe/Lisbon",
            "NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_UNIT": "kWh",
        }
        assert _state(session, env=supplied) == STATE_OK


def test_two_processes_with_the_same_contract_agree_about_the_fleet(settings, monkeypatch):
    """A regressão em si: mesma base, mesmo contrato, veredicto idêntico.

    É este o invariante que faltava. Enquanto o `web` e o `scheduler` virem o
    mesmo contrato, têm de classificar a frota da mesma maneira; a paridade de
    ambiente que o garante em produção é afirmada em
    `test_deployment_contract.py`.
    """
    factory = factory_for(settings, monkeypatch)
    contract = {
        "NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_TIMEZONE": "Europe/Lisbon",
        "NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_UNIT": "kWh",
    }
    with factory() as session:
        connection = _connection(session)
        asset, mapping = _mapped_asset(session, connection)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=TODAY - timedelta(days=1))
        _schedule(session, connection.id)
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=TODAY - timedelta(days=1))

        as_web = assess_production_coverage(session, on=TODAY, env=contract)
        as_scheduler = assess_production_coverage(session, on=TODAY, env=dict(contract))

    assert [f.state for f in as_web] == [f.state for f in as_scheduler]
    assert coverage_summary(as_web) == coverage_summary(as_scheduler)


def test_a_cursor_written_under_another_providers_key_is_still_a_cursor(settings, monkeypatch):
    """Encontrado em produção, 2026-09-07.

    Cada adaptador nomeia o seu cursor: o FusionSolar escreve
    `fusionsolar-daily-production`, o Sigenergy escreve
    `sigenergy-daily-production`. O classificador procurava a chave do
    FusionSolar, por isso o cursor do Sigenergy — actual, a sincronizar
    diariamente — era invisível, e duas centrais apareciam como "produção
    não inicializada" a pedir uma data de bootstrap a uma ligação que nunca
    precisou de uma. A capacidade é o que este módulo quer dizer.
    """
    from nemsei.providers.registry import ProviderCapability
    from nemsei.sync.models import SyncCursor

    factory = factory_for(settings, monkeypatch)
    with factory() as session:
        connection = create_connection(
            session, provider_code="sigenergy", connection_key="sigen",
            display_name="Sigenergy live", credential_reference="primary",
            enabled=True, configuration_status="configured",
        )
        asset, mapping = _mapped_asset(session, connection)
        session.add(
            SyncCursor(
                provider_connection_id=connection.id,
                capability=ProviderCapability.PRODUCTION_HISTORY.value,
                cursor_key="sigenergy-daily-production",
                checkpoint_json={"last_completed_day": (TODAY - timedelta(days=1)).isoformat()},
                covered_through=utc_now(),
                updated_at=utc_now(),
            )
        )
        _schedule(session, connection.id)
        _fact(session, asset_id=asset.id, mapping_id=mapping.id, on=TODAY - timedelta(days=1))
        session.flush()
        finding = _finding(session)

    assert finding.production_ingestion == "polling"
    assert finding.cursor_last_completed_day == TODAY - timedelta(days=1)
    assert finding.state == STATE_OK
    assert finding.state != STATE_NOT_INITIALIZED
