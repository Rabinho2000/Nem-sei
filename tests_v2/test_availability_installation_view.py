"""A WAT diária na página da instalação: o número, a origem e os buracos.

Três coisas que esta vista não pode fazer, e que aqui ficam presas:

* apresentar um dia sem medição como 0 %;
* apresentar uma figura amostrada como se fosse a WAT contratual;
* fazer uma chamada ao provider para desenhar a página.

A terceira é estrutural e por isso é testada como tal: o caminho de leitura
não importa nenhum cliente de provider em lado nenhum.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db import build_engine
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import AssetAvailabilityDaily
from nemsei.reporting.rules.availability_source import (
    KIND_CONTRACTUAL,
    KIND_OPERATIONAL,
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
    SOURCE_FUSIONSOLAR_SAMPLED,
)
from nemsei.shared.clock import utc_now
from nemsei.web.series import availability_panel
from tests_v2.test_migrations import upgrade


def login(client) -> None:
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"


def _row(session, *, asset_id, on, source, pct, valid_sample_count=47, expected=8, observed=8, coverage=None):
    now = utc_now()
    kind = KIND_CONTRACTUAL if source == SOURCE_FUSIONSOLAR_DEVICE_HISTORY else KIND_OPERATIONAL
    session.add(
        AssetAvailabilityDaily(
            asset_id=asset_id,
            availability_date=on,
            availability_pct=(Decimal(str(pct)) if pct is not None else None),
            valid_sample_count=valid_sample_count,
            expected_device_count=expected,
            observed_device_count=observed,
            minimum_required_samples=4,
            coverage_status=coverage or ("complete" if pct is not None else "indeterminate"),
            warning_codes_json=[],
            source=source,
            source_kind=kind,
            calculation_details_json={},
            calculated_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def test_panel_reports_the_last_closed_day_with_a_figure(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central com WAT")
        _row(session, asset_id=asset.id, on=yesterday, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        asset_id = asset.id

    with factory() as session:
        panel = availability_panel(session, asset_id=asset_id)

    assert panel["kpi"]["available"] is True
    assert panel["kpi"]["availability_pct"] == pytest.approx(98.72)
    assert panel["kpi"]["date"] == yesterday
    assert panel["kpi"]["kind"]["label"] == "Contratual"
    assert panel["kpi"]["valid_sample_count"] == 47


def test_a_day_without_a_figure_is_a_gap_never_a_zero(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central com buraco")
        _row(session, asset_id=asset.id, on=yesterday, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        asset_id = asset.id

    with factory() as session:
        panel = availability_panel(session, asset_id=asset_id, days=5)

    values = [bar.point.value for bar in panel["chart"].bars]
    # Quatro dias sem linha nenhuma, um dia com valor. Nem um único 0.0.
    assert values.count(None) == 4
    assert 0.0 not in values
    assert panel["days_with_value"] == 1
    assert all(bar.point.missing for bar in panel["chart"].bars if bar.point.value is None)


def test_a_sampled_day_is_never_labelled_contractual(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central so amostrada")
        _row(session, asset_id=asset.id, on=yesterday, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        asset_id = asset.id

    with factory() as session:
        panel = availability_panel(session, asset_id=asset_id, days=5)

    assert panel["kpi"]["kind"]["kind"] == KIND_OPERATIONAL
    assert panel["kpi"]["kind"]["label"] != "Contratual"
    assert "amostrada" in panel["kpi"]["kind"]["label"]
    assert panel["contractual_available"] is False
    assert panel["contractual_days"] == 0


def test_a_contractual_and_a_sampled_day_report_the_contractual_one(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central com duas origens")
        _row(session, asset_id=asset.id, on=yesterday, source=SOURCE_FUSIONSOLAR_SAMPLED, pct="91.40")
        _row(session, asset_id=asset.id, on=yesterday, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        asset_id = asset.id

    with factory() as session:
        panel = availability_panel(session, asset_id=asset_id, days=5)

    assert panel["kpi"]["availability_pct"] == pytest.approx(98.72)
    assert panel["kpi"]["kind"]["kind"] == KIND_CONTRACTUAL
    today_row = panel["rows"][1]
    assert today_row["date"] == yesterday
    assert today_row["sources_available"] == [SOURCE_FUSIONSOLAR_DEVICE_HISTORY, SOURCE_FUSIONSOLAR_SAMPLED]


def test_the_kpi_explains_an_absent_figure_instead_of_showing_a_dash_alone(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central incompleta")
        _row(
            session, asset_id=asset.id, on=yesterday, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct=None,
            expected=8, observed=7, coverage="indeterminate",
        )
        asset_id = asset.id

    with factory() as session:
        panel = availability_panel(session, asset_id=asset_id, days=5)

    assert panel["kpi"]["available"] is False
    assert panel["kpi"]["availability_pct"] is None
    assert panel["kpi"]["observed_device_count"] == 7
    assert panel["kpi"]["expected_device_count"] == 8
    assert panel["kpi"]["coverage"]["status"] == "indeterminate"


def test_the_detail_page_shows_daily_wat(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    yesterday = utc_now().date() - timedelta(days=1)
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central Visivel")
        _row(session, asset_id=asset.id, on=yesterday, source=SOURCE_FUSIONSOLAR_DEVICE_HISTORY, pct="98.72")
        asset_id = asset.id

    client = create_app(settings).test_client()
    login(client)
    response = client.get(f"/instalacoes/{asset_id}")
    assert response.status_code == 200
    assert "WAT · último dia fechado" in response.text
    assert "98,72" in response.text
    assert "WAT diária · últimos 60 dias" in response.text
    assert "Contratual" in response.text


def test_the_detail_page_shows_an_absent_wat_without_inventing_a_zero(settings, monkeypatch) -> None:
    upgrade(settings, monkeypatch)
    factory = build_session_factory(build_engine(settings))
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Central Sem WAT")
        asset_id = asset.id

    client = create_app(settings).test_client()
    login(client)
    response = client.get(f"/instalacoes/{asset_id}")
    assert response.status_code == 200
    assert "WAT · último dia fechado" in response.text
    assert "Sem disponibilidade calculada" in response.text
    assert "0,00 %" not in response.text


def test_rendering_the_availability_panel_cannot_reach_a_provider() -> None:
    """Estrutural, não por convenção: o caminho de leitura não tem cliente.

    `availability_panel` -> `asset_availability_service` -> a tabela. Se
    algum dia alguém puser uma chamada ao provider no render, este teste
    parte -- que é o ponto: um relatório ou uma página não gastam orçamento
    de chamadas de uma conta partilhada.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "nemsei"
    for module in (root / "web" / "series.py", root / "diagnostics" / "availability_service.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [name for name in imported if name.startswith("nemsei.integrations")], module.name
