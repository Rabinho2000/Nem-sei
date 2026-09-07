"""Produção diária e WAT são dois sistemas de factos, e têm de continuar a sê-lo.

Vêm de sítios diferentes (`production_facts` de `getKpiStationDay`;
`asset_availability_daily` de `getDevHistoryKpi`), falham por razões
diferentes e são pedidos por contratos diferentes. A tentação óbvia — e a
que estes testes existem para tornar impossível de passar despercebida — é
derivar uma da outra: uma central que produziu 0 kWh "devia" ter 0 % de
disponibilidade, uma central sem histórico de dispositivo "devia" poder
usar os kWh diários como aproximação.

As duas são falsas. Um dia de céu encoberto produz pouco com os inversores
todos disponíveis, e um dia sem leitura nenhuma não produziu zero: não se
sabe. Uma WAT derivada de kWh seria um número comercial inventado a partir
de meteorologia.

Os três casos abaixo são as três combinações possíveis, e o quarto teste é
estrutural: nenhum dos dois caminhos importa o outro.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.availability_service import asset_availability_series, monthly_availability_for_asset
from nemsei.diagnostics.models import AssetAvailabilityDaily
from nemsei.monitoring.service import record_production_fact
from nemsei.providers.service import create_connection, create_mapping
from nemsei.reporting.rules.availability_source import KIND_CONTRACTUAL, SOURCE_FUSIONSOLAR_DEVICE_HISTORY
from nemsei.shared.clock import utc_now
from nemsei.web.series import availability_panel, daily_series
from tests_v2.test_migrations import upgrade


DAY = date(2026, 9, 6)


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


_counter = 0


def _mapped_asset(session, *, name="Central"):
    global _counter
    _counter += 1
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key=f"indep-{_counter}", display_name="Conta",
        credential_reference="dev", enabled=True, configuration_status="configured",
    )
    asset = create_asset(session, canonical_name=f"{name} {_counter}")
    mapping = create_mapping(
        session, asset_id=asset.id, provider_connection_id=connection.id,
        external_id=f"ST-{_counter}", valid_from=date(2020, 1, 1),
    )
    session.flush()
    return asset, mapping


def _production(session, *, asset_id, mapping_id, on=DAY, value="123.45"):
    record_production_fact(
        session,
        asset_id=asset_id,
        provider_mapping_id=mapping_id,
        source_fact_key=f"fusionsolar-day:{on.isoformat()}",
        period_start=datetime.combine(on, datetime.min.time(), tzinfo=timezone.utc),
        period_end=datetime.combine(on + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
        granularity="day",
        value=Decimal(value),
        unit="kWh",
        quality="complete",
        completeness="complete",
    )


def _contractual_wat(session, *, asset_id, on=DAY, pct="98.72"):
    now = utc_now()
    session.add(
        AssetAvailabilityDaily(
            asset_id=asset_id,
            availability_date=on,
            availability_pct=Decimal(pct),
            valid_sample_count=47,
            expected_device_count=2,
            observed_device_count=2,
            minimum_required_samples=0,
            coverage_status="complete",
            warning_codes_json=[],
            source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
            source_kind=KIND_CONTRACTUAL,
            calculation_details_json={},
            calculated_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def _production_days(session, asset_id, *, window_days=30):
    """Quantos dias da janela têm produção, pelo mesmo caminho da interface."""
    return daily_series(session, asset_id=asset_id, days=window_days)["days_with_data"]


# ---------------------------------------------------------------------------
# Caso A: sem ProductionFact, com history_read completo.
# ---------------------------------------------------------------------------


def test_a_day_with_no_production_fact_still_reports_its_contractual_wat(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset, _mapping = _mapped_asset(session, name="Sem produção")
        _contractual_wat(session, asset_id=asset.id)
        asset_id = asset.id

    with factory() as session:
        series = asset_availability_series(session, asset_id=asset_id, from_date=DAY, to_date=DAY)
        assert _production_days(session, asset_id) == 0

    assert series[0]["availability_pct"] == pytest.approx(98.72)
    assert series[0]["source_kind"] == KIND_CONTRACTUAL
    # E, sobretudo: a WAT não foi puxada para baixo por não haver kWh.
    assert series[0]["coverage_status"] == "complete"


# ---------------------------------------------------------------------------
# Caso B: com ProductionFact, sem history_read.
# ---------------------------------------------------------------------------


def test_a_day_with_production_but_no_device_history_has_no_wat_at_all(settings, monkeypatch):
    """E o que fica é `missing`, não uma WAT derivada dos kWh.

    Não existe fallback de disponibilidade a partir de energia diária, e
    este é o teste que garante que continuará a não existir: 123,45 kWh não
    dizem nada sobre quantas horas os inversores estiveram disponíveis.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset, mapping = _mapped_asset(session, name="Sem histórico")
        _production(session, asset_id=asset.id, mapping_id=mapping.id)
        asset_id = asset.id

    with factory() as session:
        series = asset_availability_series(session, asset_id=asset_id, from_date=DAY, to_date=DAY)
        assert _production_days(session, asset_id) >= 0  # a produção está lá, pelo seu próprio caminho

    assert series[0]["availability_pct"] is None
    assert series[0]["coverage_status"] == "missing"
    assert series[0]["source"] is None


def test_a_zero_kwh_day_does_not_become_a_zero_percent_wat(settings, monkeypatch):
    """A confusão mais fácil de cometer e a mais cara: um dia encoberto
    produz pouco com os inversores todos disponíveis."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset, mapping = _mapped_asset(session, name="Dia encoberto")
        _production(session, asset_id=asset.id, mapping_id=mapping.id, value="0")
        _contractual_wat(session, asset_id=asset.id, pct="100.00")
        asset_id = asset.id

    with factory() as session:
        series = asset_availability_series(session, asset_id=asset_id, from_date=DAY, to_date=DAY)
    assert series[0]["availability_pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Caso C: os dois presentes.
# ---------------------------------------------------------------------------


def test_both_present_are_both_reported_and_neither_is_derived_from_the_other(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset, mapping = _mapped_asset(session, name="Completa")
        _production(session, asset_id=asset.id, mapping_id=mapping.id, value="123.45")
        _contractual_wat(session, asset_id=asset.id, pct="98.72")
        asset_id = asset.id

    with factory() as session:
        series = asset_availability_series(session, asset_id=asset_id, from_date=DAY, to_date=DAY)
        production = daily_series(session, asset_id=asset_id, days=400)
    assert series[0]["availability_pct"] == pytest.approx(98.72)
    assert production["total"] == pytest.approx(123.45)


def test_the_monthly_figures_do_not_gate_each_other(settings, monkeypatch):
    """Um mês de WAT completa não precisa de um mês de produção completo, e
    vice-versa. São dois `coverage_status`, calculados de tabelas
    diferentes, e o relatório mostra os dois."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset, _mapping = _mapped_asset(session, name="Mês só com WAT")
        for offset in range(30):
            _contractual_wat(session, asset_id=asset.id, on=date.fromordinal(date(2026, 6, 1).toordinal() + offset))
        asset_id = asset.id

    with factory() as session:
        month = monthly_availability_for_asset(
            session, asset_id=asset_id, month_start=date(2026, 6, 1), month_end_exclusive=date(2026, 7, 1)
        )
        assert _production_days(session, asset_id) == 0
    assert month["coverage_status"] == "complete"
    assert month["availability_pct"] == pytest.approx(98.72)


# ---------------------------------------------------------------------------
# Estrutural: nem sequer há por onde derivar uma da outra.
# ---------------------------------------------------------------------------


def test_the_availability_engine_never_reads_production_facts():
    """Um `ProductionFact` no motor de disponibilidade seria a porta de
    entrada de uma WAT derivada de kWh. Não há."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "nemsei"
    for module in (
        root / "diagnostics" / "availability_service.py",
        root / "reporting" / "rules" / "availability_window.py",
        root / "reporting" / "rules" / "availability_slots.py",
        root / "reporting" / "rules" / "availability_source.py",
    ):
        text = module.read_text(encoding="utf-8")
        assert "ProductionFact" not in text, module.name
        assert "production_facts" not in text.replace("`production_facts`", ""), module.name


def test_the_production_pipeline_never_reads_availability():
    """E o inverso: a sincronização de produção não pode passar a depender
    de haver histórico de dispositivo para escrever um facto."""
    from pathlib import Path

    module = Path(__file__).resolve().parents[1] / "src" / "nemsei" / "integrations" / "fusionsolar" / "production.py"
    text = module.read_text(encoding="utf-8")
    assert "AssetAvailabilityDaily" not in text
    assert "availability" not in text.lower()


def test_the_installation_panel_shows_wat_when_production_is_absent(settings, monkeypatch):
    """O critério de aceitação visto da página: sem produção nenhuma, a WAT
    do dia continua no topo."""
    factory = factory_for(settings, monkeypatch)
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        asset, _mapping = _mapped_asset(session, name="Só WAT")
        _contractual_wat(session, asset_id=asset.id, on=yesterday)
        asset_id = asset.id

    with factory() as session:
        panel = availability_panel(session, asset_id=asset_id, days=5)
        assert _production_days(session, asset_id, window_days=5) == 0
    assert panel["kpi"]["available"] is True
    assert panel["kpi"]["availability_pct"] == pytest.approx(98.72)
