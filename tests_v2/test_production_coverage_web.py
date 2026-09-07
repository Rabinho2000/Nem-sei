"""O ecrã de cobertura de produção: rende, filtra, e não dispara nada.

O que este ficheiro prende não é a presença de palavras portuguesas numa
página; é que a página responde à pergunta certa (qual o elo que partiu),
que o resumo no topo conta a frota e não o filtro, e que abrir a página
não gasta uma única chamada da conta partilhada.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db import build_engine
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.production_coverage import COVERAGE_STATES
from nemsei.jobs.models import ScheduleState
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.registry import ProviderCapability
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import SyncRun
from nemsei.web.production_coverage_queries import UNLABELLED_COVERAGE_STATES, production_coverage_page
from tests_v2.production_scheduling_fixtures import seed_production_cursor
from tests_v2.test_migrations import upgrade


def login(client) -> None:
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"


def seed_fleet(settings, monkeypatch):
    """Uma frota pequena mas mista: uma saudável, uma sem política, uma sem mapping."""
    upgrade(settings, monkeypatch)
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_TIMEZONE", "Europe/Lisbon")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_DEV_PRODUCTION_UNIT", "kWh")
    factory = build_session_factory(build_engine(settings))
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="cobertura", display_name="Conta principal",
            credential_reference="dev", enabled=True, configuration_status="configured",
        )
        session.flush()

        healthy = create_asset(session, canonical_name="Central Saudavel")
        mapping = create_mapping(
            session, asset_id=healthy.id, provider_connection_id=connection.id,
            external_id="ST-OK", valid_from=date(2020, 1, 1),
        )
        create_source_policy(
            session, asset_id=healthy.id, provider_mapping_id=mapping.id,
            source_use="production", priority=1, valid_from=date(2020, 1, 1),
        )
        record_production_fact(
            session, asset_id=healthy.id, provider_mapping_id=mapping.id,
            source_fact_key=f"fusionsolar-day:{yesterday.isoformat()}",
            period_start=datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc),
            period_end=datetime.combine(yesterday + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
            granularity="day", value=Decimal("120.5"), unit="kWh", quality="complete", completeness="complete",
        )

        orphan = create_asset(session, canonical_name="Central Sem Politica")
        create_mapping(
            session, asset_id=orphan.id, provider_connection_id=connection.id,
            external_id="ST-NOPOL", valid_from=date(2020, 1, 1),
        )

        create_asset(session, canonical_name="Central Sem Mapping")

        seed_production_cursor(session, connection_id=connection.id, last_completed_day=yesterday)
        session.add(
            ScheduleState(
                schedule_key=f"production.incremental:{connection.id}",
                next_run_at=utc_now(), updated_at=utc_now(),
            )
        )
        session.add(
            SyncRun(
                provider_connection_id=connection.id,
                capability=ProviderCapability.PRODUCTION_HISTORY.value,
                status="success", started_at=utc_now(), finished_at=utc_now(),
                completeness="complete", metadata_json={},
            )
        )
    return factory


def test_every_state_the_engine_can_return_has_a_label() -> None:
    """Um estado novo no motor não pode chegar à tabela como código cru."""
    assert UNLABELLED_COVERAGE_STATES == ()


def test_the_page_renders_and_names_the_broken_link_per_installation(settings, monkeypatch) -> None:
    seed_fleet(settings, monkeypatch)
    client = create_app(settings).test_client()
    login(client)
    response = client.get("/system/cobertura-producao")
    assert response.status_code == 200
    assert "Cobertura de produção" in response.text
    assert "Sem mapping" in response.text
    assert "Sem política de fonte" in response.text
    assert "Com produção recente" in response.text


def test_the_summary_counts_the_fleet_not_the_filter(settings, monkeypatch) -> None:
    """O número no topo responde "quantas centrais estão sem produção", nunca
    "quantas linhas estou a ver" -- que é a confusão que fazia um parque
    parecer saudável por se estar a olhar para um filtro."""
    factory = seed_fleet(settings, monkeypatch)
    with factory() as session:
        unfiltered = production_coverage_page(session)
        filtered = production_coverage_page(session, only_problems=True)
    assert unfiltered["summary"] == filtered["summary"]
    assert filtered["showing"] == 2
    assert unfiltered["showing"] == 3


def test_the_problem_filter_hides_only_the_healthy_rows(settings, monkeypatch) -> None:
    factory = seed_fleet(settings, monkeypatch)
    with factory() as session:
        filtered = production_coverage_page(session, only_problems=True)
    assert all(row["finding"].state != "ok" for row in filtered["rows"])
    assert "Central Saudavel" not in {row["finding"].asset_name for row in filtered["rows"]}


def test_the_state_filter_selects_exactly_one_condition(settings, monkeypatch) -> None:
    factory = seed_fleet(settings, monkeypatch)
    with factory() as session:
        filtered = production_coverage_page(session, state="no_provider_mapping")
    assert [row["finding"].asset_name for row in filtered["rows"]] == ["Central Sem Mapping"]


def test_rows_are_ordered_deepest_broken_link_first(settings, monkeypatch) -> None:
    """A ordem é a da cadeia: sem mapping antes de sem política, e a saudável
    no fim. Uma tabela por ordem alfabética faria o operador procurar."""
    factory = seed_fleet(settings, monkeypatch)
    with factory() as session:
        page = production_coverage_page(session)
    states = [row["finding"].state for row in page["rows"]]
    order = {value: index for index, value in enumerate(COVERAGE_STATES)}
    assert states[-1] == "ok"
    assert order[states[0]] < order[states[1]]


def test_the_page_has_no_write_action_at_all(settings, monkeypatch) -> None:
    """Nesta fase é observabilidade. Um disparo daqui gastaria orçamento de
    chamadas da conta partilhada exactamente quando ela já está a recusar --
    e as coisas que consertam estes estados têm auditoria própria noutros
    ecrãs."""
    seed_fleet(settings, monkeypatch)
    app = create_app(settings)

    # A rota em si, não o HTML: o layout base traz o formulário de logout em
    # todas as páginas, por isso procurar `method="post"` no texto respondia
    # a outra pergunta. O que interessa é que esta rota não aceita escritas.
    rules = [rule for rule in app.url_map.iter_rules() if str(rule) == "/system/cobertura-producao"]
    assert rules and set(rules[0].methods) <= {"GET", "HEAD", "OPTIONS"}

    # E que o único formulário do conteúdo é o de filtros, em GET.
    client = app.test_client()
    login(client)
    body = client.get("/system/cobertura-producao").text
    content = body[body.index("page-header"):]
    assert 'method="post"' not in content.lower()
    assert 'method="get"' in content.lower()


def test_the_page_makes_no_provider_call() -> None:
    """Estrutural: nem o read model nem o motor importam um adaptador."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "nemsei"
    for module in (root / "web" / "production_coverage_queries.py", root / "diagnostics" / "production_coverage.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [name for name in imported if name.startswith("nemsei.integrations")], module.name


def test_the_system_page_links_to_the_coverage_page(settings, monkeypatch) -> None:
    seed_fleet(settings, monkeypatch)
    client = create_app(settings).test_client()
    login(client)
    response = client.get("/system")
    assert "/system/cobertura-producao" in response.text
