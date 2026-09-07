"""Mais do que uma connection FusionSolar pode ter produção agendada.

E, tão importante como isso: nenhuma connection que ninguém ligou pode ter.
O `for connection in all_connections` que este módulo existe para não ser
está preso aqui, não por comentário.

Cada connection mantém o seu próprio agendamento, cooldown, cursor, dedupe
e observabilidade -- os testes abaixo verificam-no pelo que fica na base de
dados (`schedule_state`, `jobs.dedupe_key`, `jobs.payload_json`), porque é
isso que decide se duas contas interferem uma com a outra.
"""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta

from sqlalchemy import select

from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job, ScheduleState
from nemsei.jobs.scheduler import Scheduler
from nemsei.providers.service import create_connection
from nemsei.sync.production_scheduling import (
    MODE_BOOTSTRAP,
    MODE_INCREMENTAL,
    MODE_NOT_INITIALIZED,
    PRODUCTION_CURSOR_KEY,
    production_schedule_targets,
)
from tests_v2.production_scheduling_fixtures import seed_production_cursor
from tests_v2.test_migrations import upgrade


def _connection(session, *, key: str, enabled=True, configured=True, sync_enabled=False, initial_from=None, interval=None, provider="fusionsolar"):
    connection = create_connection(
        session,
        provider_code=provider,
        connection_key=key,
        display_name=f"Conta {key}",
        credential_reference="dev",
        enabled=enabled,
        configuration_status="configured" if configured else "not_configured",
    )
    connection.production_sync_enabled = sync_enabled
    connection.initial_production_from_date = initial_from
    connection.production_sync_interval_hours = interval
    session.flush()
    return connection


def _factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _targets(session, **overrides):
    kwargs = {"default_interval_hours": 24, "max_incremental_gap_days": 31}
    kwargs.update(overrides)
    return production_schedule_targets(session, **kwargs)


def test_the_cursor_key_matches_the_adapter_that_writes_it() -> None:
    """`nemsei.sync` may not import `nemsei.integrations`, so the constant is
    duplicated -- and this is what stops the two copies from drifting."""
    from nemsei.integrations.fusionsolar.production import _CURSOR_KEY

    assert PRODUCTION_CURSOR_KEY == _CURSOR_KEY


# ---------------------------------------------------------------------------
# Eligibilidade: explícita, por connection, nunca uma varredura.
# ---------------------------------------------------------------------------


def test_a_connection_nobody_enabled_is_never_a_target(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _connection(session, key="quieta", sync_enabled=False)
    with factory() as session:
        assert _targets(session) == []


def test_a_disabled_connection_is_never_a_target_even_when_marked(settings, monkeypatch) -> None:
    """Duas coisas diferentes: falar com a conta, e sincronizar produção nela."""
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _connection(session, key="desligada", enabled=False, sync_enabled=True)
    with factory() as session:
        assert _targets(session) == []


def test_a_sigenergy_connection_is_never_a_fusionsolar_target(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _connection(session, key="sigen", sync_enabled=True, provider="sigenergy")
    with factory() as session:
        assert _targets(session) == []


def test_two_enabled_connections_are_two_independent_targets(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    yesterday = date(2026, 9, 6)
    with factory() as session, session.begin():
        first = _connection(session, key="conta-a", sync_enabled=True)
        second = _connection(session, key="conta-b", sync_enabled=True, interval=6)
        seed_production_cursor(session, connection_id=first.id, last_completed_day=yesterday)
        seed_production_cursor(session, connection_id=second.id, last_completed_day=yesterday)
        ids = (first.id, second.id)

    with factory() as session:
        targets = _targets(session, today=date(2026, 9, 7))
    assert [target.connection_id for target in targets] == list(ids)
    assert {target.mode for target in targets} == {MODE_INCREMENTAL}
    # Cada uma com o seu intervalo e a sua chave de agendamento.
    assert [target.interval_hours for target in targets] == [24, 6]
    assert len({target.schedule_key for target in targets}) == 2


def test_the_legacy_environment_connection_is_an_extra_target_not_a_replacement(settings, monkeypatch) -> None:
    """Um deployment que ainda não pôs a coluna continua a sincronizar o mesmo."""
    factory = _factory(settings, monkeypatch)
    yesterday = date(2026, 9, 6)
    with factory() as session, session.begin():
        legacy = _connection(session, key="legado", sync_enabled=False)
        new = _connection(session, key="nova", sync_enabled=True)
        seed_production_cursor(session, connection_id=legacy.id, last_completed_day=yesterday)
        seed_production_cursor(session, connection_id=new.id, last_completed_day=yesterday)
        legacy_id, new_id = legacy.id, new.id

    with factory() as session:
        targets = _targets(session, legacy_connection_id=legacy_id, today=date(2026, 9, 7))
    assert sorted(target.connection_id for target in targets) == sorted([legacy_id, new_id])


# ---------------------------------------------------------------------------
# Modo: cursor, bootstrap, ou nada.
# ---------------------------------------------------------------------------


def test_a_connection_with_a_cursor_runs_incremental(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session, key="com-cursor", sync_enabled=True)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=date(2026, 9, 6))

    with factory() as session:
        target = _targets(session, today=date(2026, 9, 7))[0]
    assert target.mode == MODE_INCREMENTAL
    assert target.last_completed_day == date(2026, 9, 6)
    assert target.cursor_stale is False
    assert target.start_date is None


def test_a_connection_with_no_cursor_and_a_start_date_bootstraps(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _connection(session, key="por-arrancar", sync_enabled=True, initial_from=date(2026, 1, 1))

    with factory() as session:
        target = _targets(session, today=date(2026, 9, 7))[0]
    assert target.mode == MODE_BOOTSTRAP
    assert target.start_date == date(2026, 1, 1)
    assert target.last_completed_day is None


def test_a_connection_with_no_cursor_and_no_start_date_is_not_initialized(settings, monkeypatch) -> None:
    """Nada é agendado e nada é adivinhado -- e por isso nada é chamado."""
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _connection(session, key="sem-data", sync_enabled=True)

    with factory() as session:
        target = _targets(session, today=date(2026, 9, 7))[0]
    assert target.mode == MODE_NOT_INITIALIZED
    assert target.start_date is None


def test_a_cursor_further_behind_than_one_incremental_run_is_flagged_stale(settings, monkeypatch) -> None:
    """O comportamento não muda -- o job continua a correr e a falhar alto.

    O que muda é que a condição passa a ter nome, para o ecrã de cobertura
    poder dizer que a janela excede o limite e recomendar o backfill em vez
    de deixar o operador a decifrar a mensagem do serviço.
    """
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session, key="atrasada", sync_enabled=True)
        seed_production_cursor(session, connection_id=connection.id, last_completed_day=date(2026, 6, 1))

    with factory() as session:
        target = _targets(session, today=date(2026, 9, 7))[0]
    assert target.mode == MODE_INCREMENTAL
    assert target.cursor_stale is True


def test_an_unreadable_checkpoint_is_treated_as_no_cursor(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session, key="checkpoint-mau", sync_enabled=True, initial_from=date(2026, 1, 1))
        cursor = seed_production_cursor(session, connection_id=connection.id, last_completed_day=date(2026, 9, 6))
        cursor.checkpoint_json = {"last_completed_day": "não é uma data"}

    with factory() as session:
        target = _targets(session, today=date(2026, 9, 7))[0]
    assert target.mode == MODE_BOOTSTRAP


# ---------------------------------------------------------------------------
# O scheduler real, com duas connections FusionSolar.
# ---------------------------------------------------------------------------


def test_two_connections_both_receive_jobs_without_interfering(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    yesterday = date.today() - timedelta(days=1)
    with factory() as session, session.begin():
        first = _connection(session, key="frota-a", sync_enabled=True)
        second = _connection(session, key="frota-b", sync_enabled=True)
        seed_production_cursor(session, connection_id=first.id, last_completed_day=yesterday)
        seed_production_cursor(session, connection_id=second.id, last_completed_day=yesterday)
        ids = sorted([first.id, second.id])

    scheduler = Scheduler(
        dataclasses.replace(settings, production_sync_scheduler_enabled=True, production_sync_scheduler_interval_hours=24),
        owner_token="scheduler-two-connections",
    )
    assert scheduler.run_once() is True
    # Segunda passagem: nenhuma das duas volta a entrar na fila no mesmo slot.
    assert scheduler.run_once() is False

    with factory() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "production.incremental")).all()
        schedules = session.scalars(
            select(ScheduleState).where(ScheduleState.schedule_key.like("production.incremental:%"))
        ).all()
    assert sorted(job.payload_json["connection_id"] for job in jobs) == ids
    # Um agendamento por connection, e chaves de dedupe distintas: uma
    # connection não pode consumir o slot da outra.
    assert len(schedules) == 2
    assert len({job.dedupe_key for job in jobs}) == 2


def test_one_connection_bootstraps_while_the_other_runs_incremental(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    yesterday = date.today() - timedelta(days=1)
    with factory() as session, session.begin():
        running = _connection(session, key="a-correr", sync_enabled=True)
        fresh = _connection(session, key="acabada-de-ligar", sync_enabled=True, initial_from=date(2026, 8, 1))
        seed_production_cursor(session, connection_id=running.id, last_completed_day=yesterday)
        running_id, fresh_id = running.id, fresh.id

    scheduler = Scheduler(
        dataclasses.replace(settings, production_sync_scheduler_enabled=True),
        owner_token="scheduler-bootstrap-mix",
    )
    assert scheduler.run_once() is True

    with factory() as session:
        incremental = session.scalars(select(Job).where(Job.job_type == "production.incremental")).all()
        backfill = session.scalars(select(Job).where(Job.job_type == "production.bounded_backfill")).all()

    assert [job.payload_json["connection_id"] for job in incremental] == [running_id]
    assert [job.payload_json["connection_id"] for job in backfill] == [fresh_id]
    assert backfill[0].payload_json["bootstrap"] is True
    assert backfill[0].payload_json["start_date"] == "2026-08-01"
    # Sem `end_date`: o fim da janela é ontem em hora local do provider e
    # só o serviço, que carrega o contrato, o pode resolver.
    assert "end_date" not in backfill[0].payload_json
    # E perde a corrida ao incremental de hoje, que corre a 100.
    assert backfill[0].priority > incremental[0].priority


def test_an_uninitialised_connection_is_scheduled_nothing_at_all(settings, monkeypatch) -> None:
    """O critério de aceitação: ligar o scheduler não faz chamadas ao provider
    numa connection que ninguém disse onde começa."""
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _connection(session, key="nunca-inicializada", sync_enabled=True)

    scheduler = Scheduler(
        dataclasses.replace(settings, production_sync_scheduler_enabled=True),
        owner_token="scheduler-uninitialised",
    )
    scheduler.run_once()

    with factory() as session:
        production_jobs = session.scalars(
            select(Job).where(Job.job_type.in_(["production.incremental", "production.bounded_backfill"]))
        ).all()
        schedules = session.scalars(
            select(ScheduleState).where(ScheduleState.schedule_key.like("production.%"))
        ).all()
    assert production_jobs == []
    assert schedules == []


def test_the_bootstrap_schedule_is_idempotent_across_ticks(settings, monkeypatch) -> None:
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        _connection(session, key="idempotente", sync_enabled=True, initial_from=date(2026, 8, 1))

    scheduler = Scheduler(
        dataclasses.replace(settings, production_sync_scheduler_enabled=True),
        owner_token="scheduler-bootstrap-idempotent",
    )
    scheduler.run_once()
    scheduler.run_once()
    scheduler.run_once()

    with factory() as session:
        backfill = session.scalars(select(Job).where(Job.job_type == "production.bounded_backfill")).all()
    assert len(backfill) == 1


def test_the_bootstrap_stops_once_a_cursor_exists(settings, monkeypatch) -> None:
    """`cursor criado -> production.incremental passa a operar normalmente`."""
    factory = _factory(settings, monkeypatch)
    with factory() as session, session.begin():
        connection = _connection(session, key="arrancou", sync_enabled=True, initial_from=date(2026, 8, 1))
        connection_id = connection.id

    with factory() as session:
        assert _targets(session)[0].mode == MODE_BOOTSTRAP

    with factory() as session, session.begin():
        seed_production_cursor(session, connection_id=connection_id, last_completed_day=date.today() - timedelta(days=1))

    with factory() as session:
        assert _targets(session)[0].mode == MODE_INCREMENTAL
