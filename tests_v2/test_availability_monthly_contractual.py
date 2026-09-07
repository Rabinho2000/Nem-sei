"""A regra mensal contratual, documentada em vez de assumida.

Nada aqui altera comportamento: estes testes existem para que a regra
comercial deixe de estar só no código e passe a estar presa. Duas coisas,
em concreto:

1. **O portão**: o mês só declara `complete` — e só então publica uma WAT —
   quando existem *todos* os dias esperados e *todos* são `complete`.
   29 de 30 dá `partial` e figura nenhuma. Zero dias dá `missing`. A regra é
   a de V1 (`sampled_month_quality`'s `final`), não foi tocada, e este
   ficheiro é o que impede que alguém a "melhore" para uma média sobre os
   dias que houver.

2. **A fórmula**: a fonte contratual agrega
   `SUM(pct × valid_slots) / SUM(valid_slots)`, ponderada pela evidência que
   cada dia carrega — a query que alimentava os relatórios e o fecho mensal
   do V1 (`get_monthly_availability`). *Não* é média aritmética. Um dia com
   duas leituras utilizáveis não pode pesar o mesmo que um dia inteiro, e
   trocar a fórmula mudaria em silêncio todos os meses já emitidos.

A média aritmética continua a ser a regra da fonte *operacional*, porque
era a de V1 para o motor amostrado. As duas coexistem de propósito: o V1
agregava os seus dois motores ao mês de maneiras diferentes e escolher uma
para ambos seria inventar um número.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.availability_service import monthly_availability_for_asset
from nemsei.diagnostics.models import AssetAvailabilityDaily
from nemsei.reporting.rules.availability_source import (
    KIND_CONTRACTUAL,
    KIND_OPERATIONAL,
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
    SOURCE_FUSIONSOLAR_SAMPLED,
)
from nemsei.shared.clock import utc_now
from tests_v2.test_migrations import upgrade


MONTH_START = date(2026, 6, 1)
MONTH_END = date(2026, 7, 1)
DAYS_IN_MONTH = 30


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def _day(session, *, asset_id, on, source, pct, valid_slots, coverage_status=None):
    now = utc_now()
    kind = KIND_CONTRACTUAL if source == SOURCE_FUSIONSOLAR_DEVICE_HISTORY else KIND_OPERATIONAL
    session.add(
        AssetAvailabilityDaily(
            asset_id=asset_id,
            availability_date=on,
            availability_pct=(Decimal(str(pct)) if pct is not None else None),
            valid_sample_count=valid_slots,
            expected_device_count=2,
            observed_device_count=2,
            minimum_required_samples=0,
            coverage_status=coverage_status or ("complete" if pct is not None else "indeterminate"),
            warning_codes_json=[],
            source=source,
            source_kind=kind,
            calculation_details_json={},
            calculated_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def _contractual_month(session, asset_id, *, days, pct="100.00", valid_slots=48):
    for offset in range(days):
        _day(
            session,
            asset_id=asset_id,
            on=date.fromordinal(MONTH_START.toordinal() + offset),
            source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
            pct=pct,
            valid_slots=valid_slots,
        )


def _monthly(session, asset_id):
    return monthly_availability_for_asset(
        session, asset_id=asset_id, month_start=MONTH_START, month_end_exclusive=MONTH_END
    )


# ---------------------------------------------------------------------------
# O portão: 30/30, 29/30, 0/30.
# ---------------------------------------------------------------------------


def test_thirty_of_thirty_complete_days_publish_a_monthly_wat(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Mês inteiro")
        _contractual_month(session, asset.id, days=DAYS_IN_MONTH, pct="98.00")
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["coverage_status"] == "complete"
    assert month["availability_pct"] == pytest.approx(98.0)
    assert month["covered_days"] == DAYS_IN_MONTH
    assert month["expected_days"] == DAYS_IN_MONTH
    assert month["source_kind"] == KIND_CONTRACTUAL


def test_twenty_nine_of_thirty_is_partial_with_no_figure_at_all(settings, monkeypatch):
    """Um dia em falta não vira "a média dos 29 que houve".

    A regra não muda sem evidência: um mês incompleto que publicasse um
    número seria indistinguível de um mês completo no relatório do cliente,
    e é essa distinção que o `final` de V1 existia para preservar.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Falta um dia")
        _contractual_month(session, asset.id, days=DAYS_IN_MONTH - 1, pct="98.00")
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["coverage_status"] == "partial"
    assert month["availability_pct"] is None
    assert month["covered_days"] == DAYS_IN_MONTH - 1


def test_one_incomplete_day_out_of_thirty_is_also_partial(settings, monkeypatch):
    """O portão tem duas condições, não uma: todos os dias *e* todos completos."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Um dia indeterminado")
        _contractual_month(session, asset.id, days=DAYS_IN_MONTH - 1, pct="98.00")
        _day(
            session, asset_id=asset.id, on=date(2026, 6, 30),
            source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct=None, valid_slots=0,
            coverage_status="indeterminate",
        )
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["coverage_status"] == "partial"
    assert month["availability_pct"] is None
    assert month["covered_days"] == DAYS_IN_MONTH


def test_zero_days_is_missing_not_partial(settings, monkeypatch):
    """O desvio deliberado face a V1, que devolvia `sampled_partial` mesmo
    para um mês com zero linhas -- não tinha terceiro estado onde cair."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Mês vazio")
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["coverage_status"] == "missing"
    assert month["availability_pct"] is None
    assert month["covered_days"] == 0
    assert month["source"] is None


# ---------------------------------------------------------------------------
# A fórmula: ponderada por valid_slots, nunca aritmética.
# ---------------------------------------------------------------------------


def test_the_contractual_month_weights_each_day_by_its_valid_slots(settings, monkeypatch):
    """A diferença entre as duas fórmulas, num mês construído para as separar.

    Vinte e nove dias a 100 % com 48 slots, e um dia a 50 % com 4 slots:

    * ponderada  = (29×100×48 + 50×4) / (29×48 + 4) = 99,29 %
    * aritmética = (29×100 + 50) / 30               = 98,33 %

    Se alguém trocar a fórmula, este teste diz qual das duas passou a
    correr — e o número que muda é o que vai na fatura.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Dia curto")
        _contractual_month(session, asset.id, days=DAYS_IN_MONTH - 1, pct="100.00", valid_slots=48)
        _day(
            session, asset_id=asset.id, on=date(2026, 6, 30),
            source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="50.00", valid_slots=4,
        )
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)

    weighted = (29 * 100.0 * 48 + 50.0 * 4) / (29 * 48 + 4)
    arithmetic = (29 * 100.0 + 50.0) / 30
    assert month["availability_pct"] == pytest.approx(round(weighted, 2))
    assert month["availability_pct"] != pytest.approx(round(arithmetic, 2))


def test_the_operational_month_stays_an_arithmetic_mean(settings, monkeypatch):
    """V1 agregava os seus dois motores ao mês de maneiras diferentes, e a
    diferença não é cosmética -- por isso a fonte amostrada continua a média
    aritmética que era a sua, e não herda a ponderação da contratual."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Amostrada")
        for offset in range(DAYS_IN_MONTH - 1):
            _day(
                session, asset_id=asset.id, on=date.fromordinal(MONTH_START.toordinal() + offset),
                source=SOURCE_FUSIONSOLAR_SAMPLED, pct="100.00", valid_slots=48,
            )
        _day(
            session, asset_id=asset.id, on=date(2026, 6, 30),
            source=SOURCE_FUSIONSOLAR_SAMPLED, pct="50.00", valid_slots=4,
        )
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["source_kind"] == KIND_OPERATIONAL
    assert month["availability_pct"] == pytest.approx(round((29 * 100.0 + 50.0) / 30, 2))


def test_a_complete_contractual_month_outranks_a_complete_operational_one(settings, monkeypatch):
    """As duas fontes podem fechar o mesmo mês. A contratual ganha sempre, e
    não por ter sido calculada depois."""
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Duas fontes, mês inteiro")
        _contractual_month(session, asset.id, days=DAYS_IN_MONTH, pct="98.00")
        for offset in range(DAYS_IN_MONTH):
            _day(
                session, asset_id=asset.id, on=date.fromordinal(MONTH_START.toordinal() + offset),
                source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.00", valid_slots=48,
            )
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["source"] == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
    assert month["availability_pct"] == pytest.approx(98.0)


def test_an_incomplete_contractual_month_does_not_let_the_operational_one_stand_in(settings, monkeypatch):
    """O caso que mais interessa não errar.

    A contratual está incompleta e a amostrada está inteira. A amostrada é
    reportada -- é a única figura que existe -- mas **rotulada como
    operacional**. O que nunca pode acontecer é o mês sair com um número
    amostrado sob a etiqueta contratual, que é como uma estimativa de sonda
    chegaria a um contrato.
    """
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Contratual a meio")
        _contractual_month(session, asset.id, days=10, pct="98.00")
        for offset in range(DAYS_IN_MONTH):
            _day(
                session, asset_id=asset.id, on=date.fromordinal(MONTH_START.toordinal() + offset),
                source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.00", valid_slots=48,
            )
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["availability_pct"] == pytest.approx(91.0)
    assert month["source"] == SOURCE_FUSIONSOLAR_SAMPLED
    assert month["source_kind"] == KIND_OPERATIONAL


def test_neither_source_complete_reports_the_contractual_account_of_the_absence(settings, monkeypatch):
    factory = factory_for(settings, monkeypatch)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Nenhuma fechada")
        _contractual_month(session, asset.id, days=10, pct="98.00")
        for offset in range(5):
            _day(
                session, asset_id=asset.id, on=date.fromordinal(MONTH_START.toordinal() + offset),
                source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.00", valid_slots=48,
            )
        asset_id = asset.id
    with factory() as session:
        month = _monthly(session, asset_id)
    assert month["availability_pct"] is None
    assert month["coverage_status"] == "partial"
    assert month["source"] == SOURCE_FUSIONSOLAR_DEVICE_HISTORY
    assert month["covered_days"] == 10
