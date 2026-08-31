"""Bloco C: the commercial inputs a report needs are reachable from a browser.

`import_financial_model`, `set_tariff` and `set_billing_config` were written
and tested long before any route touched them. Production still shows it: 0
financial models, 1 tariff and 1 billing configuration for 266 assets, which is
exactly why expected production has nowhere to come from.
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.reporting.commercial_models import AssetBillingConfig, AssetTariff
from nemsei.reporting.models import FinancialModel, ReportSourceFile
from tests_v2.test_financial_model_import import as_sold_workbook
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch, tmp_path):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    asset = create_asset(session, canonical_name="Central Comercial", timezone="Europe/Lisbon")
    session.commit()
    asset_id = asset.id
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"], browser["csrf_token"] = True, "admin", "test"
    workbook_bytes = as_sold_workbook(Path(tmp_path) / "modelo.xlsx").read_bytes()
    return client, asset_id, workbook_bytes


def upload(client, asset_id: int, payload: bytes, filename: str = "modelo.xlsx"):
    return client.post(
        f"/assets/{asset_id}/financial-model",
        data={"csrf_token": "test", "workbook": (BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )


def models(settings, asset_id: int) -> list[FinancialModel]:
    session = build_session_factory(build_engine(settings))()
    try:
        rows = list(session.scalars(select(FinancialModel).where(FinancialModel.asset_id == asset_id).order_by(FinancialModel.version)))
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def test_uploading_a_workbook_creates_a_draft_and_keeps_the_bytes(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, payload = seeded(settings, monkeypatch, tmp_path)

    response = upload(client, asset_id, payload)

    assert response.status_code == 302
    stored = models(settings, asset_id)
    assert len(stored) == 1
    # Never confirmed on upload: a model nobody has read must not start driving
    # a customer's expected production.
    assert stored[0].status == "draft"
    assert stored[0].detected_name == "Projeto Exemplo"

    session = build_session_factory(build_engine(settings))()
    source = session.scalar(select(ReportSourceFile).where(ReportSourceFile.asset_id == asset_id))
    # The container has no writable storage, so the database is the only copy.
    assert source.content == payload
    assert source.size_bytes == len(payload)
    session.close()


def test_the_preview_shows_identity_warnings_and_the_twelve_months(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, payload = seeded(settings, monkeypatch, tmp_path)
    upload(client, asset_id, payload)
    model_id = models(settings, asset_id)[0].id

    page = client.get(f"/financial-models/{model_id}")

    assert page.status_code == 200
    assert "Projeto Exemplo" in page.text
    assert "Rascunho" in page.text
    assert "Produção esperada por mês" in page.text
    assert "Confirmar modelo" in page.text
    for month in ("Jan", "Jun", "Dez"):
        assert f">{month}<" in page.text


def test_confirming_promotes_the_draft_and_supersedes_the_previous(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, payload = seeded(settings, monkeypatch, tmp_path)
    upload(client, asset_id, payload)
    first = models(settings, asset_id)[0].id
    client.post(f"/financial-models/{first}/confirm", data={"csrf_token": "test"})

    # A second workbook for the same asset: same shape, one value changed, so
    # its hash differs and it is a genuinely new file rather than a re-import.
    second_path = as_sold_workbook(Path(tmp_path) / "outro.xlsx")
    book = load_workbook(second_path)
    book["Projeto"]["H14"] = 2345.6
    book.save(second_path)
    second_bytes = second_path.read_bytes()
    upload(client, asset_id, second_bytes, "outro.xlsx")
    second = models(settings, asset_id)[1].id
    client.post(f"/financial-models/{second}/confirm", data={"csrf_token": "test"})

    stored = {model.id: model.status for model in models(settings, asset_id)}
    assert stored[first] == "superseded"
    assert stored[second] == "confirmed"


def test_download_returns_the_exact_uploaded_file(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, payload = seeded(settings, monkeypatch, tmp_path)
    upload(client, asset_id, payload)
    model_id = models(settings, asset_id)[0].id

    response = client.get(f"/financial-models/{model_id}/download")

    assert response.status_code == 200
    assert response.data == payload
    assert "modelo.xlsx" in response.headers["Content-Disposition"]


def test_a_non_workbook_upload_is_refused_without_creating_anything(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    upload(client, asset_id, b"nao sou um livro", "notas.txt")

    assert models(settings, asset_id) == []


def test_asset_page_says_plainly_when_there_is_no_model(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    page = client.get(f"/assets/{asset_id}")

    assert "Sem modelo financeiro." in page.text
    assert "Sem modelo confirmado" in page.text
    assert "Sem tarifa registada" in page.text
    assert "Sem configuração comercial" in page.text
    # The three values an ESCO contract is made of, named the way the
    # business names them rather than after their columns.
    for label in ("Taxa de venda", "Taxa de poupança", "Venda de excedente"):
        assert label in page.text


def test_saving_a_tariff_then_replacing_it_closes_the_first(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    client.post(f"/assets/{asset_id}/tariff", data={"csrf_token": "test", "tariff_type": "simple", "valid_from": "2026-01-01", "price_simple": "0,1450"})
    client.post(
        f"/assets/{asset_id}/tariff",
        data={"csrf_token": "test", "tariff_type": "tri-hourly", "valid_from": "2026-07-01", "price_ponta": "0.21", "price_cheia": "0.17", "price_vazio": "0.11"},
    )

    session = build_session_factory(build_engine(settings))()
    rows = list(session.scalars(select(AssetTariff).where(AssetTariff.asset_id == asset_id).order_by(AssetTariff.valid_from)))
    assert len(rows) == 2
    # A comma decimal is what a Portuguese keyboard produces.
    assert rows[0].simple_price_eur_kwh == Decimal("0.145")
    assert rows[0].valid_to.isoformat() == "2026-07-01"
    assert rows[1].valid_to is None
    session.close()


def test_saving_a_billing_configuration_records_the_report_type(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    client.post(
        f"/assets/{asset_id}/billing",
        data={
            "csrf_token": "test", "report_type": "esco", "valid_from": "2026-01-01",
            "billing_mode": "energy", "billing_energy_base": "self_consumption",
            "solcor_price_per_kwh": "0.08",
            "default_electricity_price": "0.06",
            "default_export_price": "0.045",
            "export_revenue_enabled": "on",
        },
    )

    session = build_session_factory(build_engine(settings))()
    config = session.scalar(select(AssetBillingConfig).where(AssetBillingConfig.asset_id == asset_id))
    assert config.report_type == "esco"
    # All three rates land, not only the one the form used to carry an input for.
    assert config.solcor_price_per_kwh == Decimal("0.08")
    assert config.default_electricity_price == Decimal("0.06")
    assert config.default_export_price == Decimal("0.045")
    assert config.solcor_price_per_kwh == Decimal("0.08")
    assert config.created_by == "admin"
    session.close()


def test_an_invalid_report_type_is_refused(settings, monkeypatch, tmp_path) -> None:
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    client.post(f"/assets/{asset_id}/billing", data={"csrf_token": "test", "report_type": "banana", "valid_from": "2026-01-01"})

    session = build_session_factory(build_engine(settings))()
    assert session.scalar(select(AssetBillingConfig).where(AssetBillingConfig.asset_id == asset_id)) is None
    session.close()


def test_an_incomplete_cycle_tariff_is_refused_with_a_readable_reason(settings, monkeypatch, tmp_path) -> None:
    # A tri-hourly tariff without vazio and cheia violates
    # ck_asset_tariffs_cycle_prices. Before this it surfaced as a blank HTTP
    # 500; it has to name the missing field instead.
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    response = client.post(
        f"/assets/{asset_id}/tariff",
        data={"csrf_token": "test", "tariff_type": "tri-hourly", "valid_from": "2026-01-01", "price_ponta": "0.21"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Vazio" in response.text and "Cheia" in response.text
    session = build_session_factory(build_engine(settings))()
    assert session.scalar(select(AssetTariff).where(AssetTariff.asset_id == asset_id)) is None
    session.close()


def test_an_esco_saved_without_its_rates_is_refused_rather_than_zeroed(settings, monkeypatch, tmp_path) -> None:
    """The form used to have no input for two of the three rates at all.

    Posting it wrote `0` into both, and `calculate_billing` then told the
    customer they had saved 0,00 EUR -- a statement about their month rather
    than about a field nobody filled in. Refusing the save is the only reading
    that keeps missing and zero apart.
    """
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    page = client.post(
        f"/assets/{asset_id}/billing",
        data={
            "csrf_token": "test", "report_type": "esco", "valid_from": "2026-01-01",
            "billing_mode": "energy", "billing_energy_base": "self_consumption",
            "solcor_price_per_kwh": "0.08",
        },
        follow_redirects=True,
    )

    session = build_session_factory(build_engine(settings))()
    assert session.scalar(select(AssetBillingConfig).where(AssetBillingConfig.asset_id == asset_id)) is None
    assert "Taxa de poupança" in page.text


def test_an_epc_does_not_have_to_state_esco_rates(settings, monkeypatch, tmp_path) -> None:
    """The validation is ESCO's, not everyone's. An EPC invoices no energy."""
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    client.post(
        f"/assets/{asset_id}/billing",
        data={
            "csrf_token": "test", "report_type": "epc", "valid_from": "2026-01-01",
            "billing_mode": "energy", "billing_energy_base": "self_consumption",
        },
    )

    session = build_session_factory(build_engine(settings))()
    config = session.scalar(select(AssetBillingConfig).where(AssetBillingConfig.asset_id == asset_id))
    assert config is not None
    assert config.report_type == "epc"


def test_an_esco_on_an_avenca_needs_the_avenca_and_not_a_taxa_de_venda(settings, monkeypatch, tmp_path) -> None:
    """The advanced arrangement stays supported, and asks for what it uses."""
    client, asset_id, _ = seeded(settings, monkeypatch, tmp_path)

    client.post(
        f"/assets/{asset_id}/billing",
        data={
            "csrf_token": "test", "report_type": "esco", "valid_from": "2026-01-01",
            "billing_mode": "fixed_monthly_fee", "billing_energy_base": "self_consumption",
            "fixed_monthly_fee_eur": "250",
            "default_electricity_price": "0.06",
        },
    )

    session = build_session_factory(build_engine(settings))()
    config = session.scalar(select(AssetBillingConfig).where(AssetBillingConfig.asset_id == asset_id))
    assert config is not None
    assert config.fixed_monthly_fee_eur == Decimal("250")
    assert config.solcor_price_per_kwh == Decimal("0")
