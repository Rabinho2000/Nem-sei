from __future__ import annotations

import pytest
from flask import Response

import app as app_module
from app import app as flask_app
from app import ensure_database
from monitoring_board.db import get_db


def _seed_report_asset(db_path) -> int:
    ensure_database(str(db_path))
    conn = get_db(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO assets (project_name, installation_group, active_contract, kwp, contract_type)
            VALUES ('Central Rota', 'Central Rota', 'yes', '50', 'ESCO')
            """
        )
        asset_id = int(conn.execute("SELECT id FROM assets WHERE project_name = 'Central Rota'").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO asset_integrations (asset_id, provider, external_id, external_name, enabled)
            VALUES (?, 'FusionSolar', 'S1', 'Central Rota FS', 1)
            """,
            (asset_id,),
        )
        conn.commit()
        return asset_id
    finally:
        conn.close()


@pytest.fixture()
def exports_client(tmp_path):
    db_path = tmp_path / "exports.db"
    asset_id = _seed_report_asset(db_path)
    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "admin"
        sess["csrf_token"] = "token"
    try:
        yield client, asset_id
    finally:
        flask_app.config["DATABASE"] = previous_db


def test_exports_get_preserves_old_monthly_report_month_url(exports_client) -> None:
    client, asset_id = exports_client

    response = client.get(f"/exports?asset_id={asset_id}&report_month=2026-03&electricity_price=0.21&sell_price=0.04")

    assert response.status_code == 200
    assert b'value="2026-03"' in response.data
    assert b'value="monthly" selected' in response.data
    assert b'value="0.21"' in response.data
    assert b"Central Rota" in response.data


@pytest.mark.parametrize(
    ("form_values", "expected_type", "expected_label", "expected_months"),
    [
        ({"period_type": "monthly", "report_month": "2026-03"}, "monthly", "Marco 2026", 1),
        ({"period_type": "quarterly", "report_year": "2026", "report_quarter": "2"}, "quarterly", "T2 2026", 3),
        ({"period_type": "semiannual", "report_year": "2025", "report_semester": "2"}, "semiannual", "S2 2025", 6),
        ({"period_type": "annual", "report_year": "2026"}, "annual", "2026", 12),
    ],
)
def test_exports_post_passes_valid_periods_to_report_builder(
    exports_client,
    monkeypatch: pytest.MonkeyPatch,
    form_values: dict[str, str],
    expected_type: str,
    expected_label: str,
    expected_months: int,
) -> None:
    client, asset_id = exports_client
    captured = {}

    def fake_builder(_conn, **kwargs):
        period = kwargs["period"]
        captured.update(
            period_type=period.period_type.value,
            label=period.label,
            month_count=period.month_count,
            asset_id=kwargs["asset_id"],
            electricity_price=kwargs["electricity_price"],
            sell_price=kwargs["sell_price"],
            solcor_price_per_kwh=kwargs["solcor_price_per_kwh"],
        )
        return {"ok": True}

    monkeypatch.setattr(app_module, "build_fusionsolar_customer_production_report", fake_builder)
    monkeypatch.setattr(app_module, "export_customer_production_pdf", lambda _report: Response(b"%PDF-test", mimetype="application/pdf"))

    response = client.post(
        "/exports",
            data={
                "csrf_token": "token",
                "asset_id": str(asset_id),
                "billing_values_source": "manual",
                "billing_mode": "energy",
                "billing_energy_base": "self_consumption",
                "electricity_price": "0.22",
                "sell_price": "0.05",
                "solcor_price_per_kwh": "0.09",
            **form_values,
        },
    )

    assert response.status_code == 200
    assert response.data.startswith(b"%PDF-")
    assert captured == {
        "period_type": expected_type,
        "label": expected_label,
        "month_count": expected_months,
        "asset_id": asset_id,
        "electricity_price": 0.22,
        "sell_price": 0.05,
        "solcor_price_per_kwh": 0.09,
    }


def test_exports_checkbox_adds_availability_to_individual_pdf(
    exports_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, asset_id = exports_client
    captured = {}

    def fake_builder(_conn, **kwargs):
        return {"asset": {"asset_id": kwargs["asset_id"], "project_name": "Central Rota"}, "ok": True}

    def fake_availability(_conn, report, *, asset_id, period):
        report["include_availability_kpi"] = True
        report["availability_pct"] = 98.5
        captured.update(asset_id=asset_id, period_start=period.start.isoformat())
        return True

    def fake_export(report):
        captured.update(report=report)
        return Response(b"%PDF-test", mimetype="application/pdf")

    monkeypatch.setattr(app_module, "build_fusionsolar_customer_production_report", fake_builder)
    monkeypatch.setattr(app_module, "add_customer_report_availability", fake_availability)
    monkeypatch.setattr(app_module, "export_customer_production_pdf", fake_export)

    response = client.post(
        "/exports",
        data={
            "csrf_token": "token",
            "asset_id": str(asset_id),
            "period_type": "monthly",
            "report_month": "2026-03",
            "billing_values_source": "manual",
            "billing_mode": "energy",
            "billing_energy_base": "self_consumption",
            "electricity_price": "0.22",
            "sell_price": "0.05",
            "solcor_price_per_kwh": "0.09",
            "include_availability": "on",
        },
    )

    assert response.status_code == 200
    assert captured["asset_id"] == asset_id
    assert captured["period_start"] == "2026-03-01"
    assert captured["report"]["include_availability_kpi"] is True
    assert captured["report"]["availability_pct"] == 98.5


def test_exports_post_rejects_invalid_quarter_and_preserves_asset(exports_client) -> None:
    client, asset_id = exports_client

    response = client.post(
        "/exports",
        data={
            "csrf_token": "token",
            "asset_id": str(asset_id),
            "period_type": "quarterly",
            "report_year": "2026",
            "report_quarter": "5",
            "electricity_price": "0.22",
            "sell_price": "0.05",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert f"asset_id={asset_id}".encode() in response.headers["Location"].encode()
    assert b"report_quarter=5" in response.headers["Location"].encode()
