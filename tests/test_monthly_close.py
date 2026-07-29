from __future__ import annotations

from monitoring_board.reporting.monthly_close import evaluate_close_payload
from monitoring_board.reporting.quality_gate import BLOCKED, READY, WARNING


def complete_row(asset_id: int = 1) -> dict:
    return {
        "asset_id": asset_id,
        "customer_id": 1,
        "installation": f"Central {asset_id}",
        "mapping_status": "mapped",
        "energy_provider": "FusionSolar",
        "production_quality_status": "complete",
        "production_source": "monthly",
        "production_kwh": "100.00",
        "installed_power_kwp": "10.00",
        "capacity_ambiguous": False,
        "billing_config_valid": True,
        "tariff_valid": True,
        "invoice_status": "confirmed",
        "availability_pct": None,
    }


def evaluate(payload: dict, **requirements):
    return evaluate_close_payload(
        payload,
        scope=requirements.pop("scope", "individual"),
        requires_financials=requirements.pop("requires_financials", False),
        requires_availability=requirements.pop("requires_availability", False),
        requires_customer=requirements.pop("requires_customer", False),
    )


def test_ready_installation_with_optional_availability_has_warning() -> None:
    result = evaluate(complete_row())
    assert result.status == WARNING
    assert {item.code for item in result.warnings} == {"missing_availability"}
    assert not result.blockers


def test_complete_portfolio_nested_rows_are_evaluated() -> None:
    payload = {
        "rows": [
            {"asset_id": 1, "values": {**complete_row(1), "availability_pct": "99"}},
            {"asset_id": 2, "values": {**complete_row(2), "availability_pct": "98"}},
        ]
    }
    result = evaluate(payload, scope="portfolio", requires_customer=True)
    assert result.status == READY


def test_mapping_pending_blocks_approval_quality() -> None:
    result = evaluate({**complete_row(), "mapping_status": "mapping_pending"})
    assert result.status == BLOCKED
    assert "mapping_pending" in {item.code for item in result.blockers}


def test_partial_and_conflicting_production_are_blocked() -> None:
    for status in ("partial", "conflict"):
        result = evaluate({**complete_row(), "production_quality_status": status})
        assert result.status == BLOCKED
        assert f"production_{status}" in {item.code for item in result.blockers}


def test_financial_requirements_control_billing_and_tariff_severity() -> None:
    payload = {
        **complete_row(),
        "billing_config_valid": False,
        "tariff_valid": False,
        "availability_pct": "99",
    }
    assert evaluate(payload).status == READY
    required = evaluate(payload, requires_financials=True)
    assert required.status == BLOCKED
    assert {item.code for item in required.blockers} == {
        "missing_billing_config",
        "missing_tariff",
    }


def test_required_availability_and_customer_are_blocked() -> None:
    payload = {**complete_row(), "customer_id": None, "availability_pct": None}
    result = evaluate(
        payload,
        scope="portfolio",
        requires_availability=True,
        requires_customer=True,
    )
    assert result.status == BLOCKED
    assert {"missing_availability", "missing_customer"} <= {
        item.code for item in result.blockers
    }


def test_sigenergy_unconfirmed_history_cannot_be_approved() -> None:
    payload = {
        **complete_row(),
        "energy_provider": "Sigenergy",
        "sigenergy_history_status": "missing_permission",
        "sigenergy_energy_unit_confirmed": False,
        "availability_pct": "99",
    }
    result = evaluate(payload)
    assert result.status == BLOCKED
    assert {
        "sigenergy_history_forbidden",
        "sigenergy_energy_unit_unconfirmed",
    } <= {item.code for item in result.blockers}
