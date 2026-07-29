from monitoring_board.reporting.quality_gate import BLOCKED, READY, evaluate_report_quality


def base_payload(provider: str) -> dict:
    return {
        "asset_id": 1,
        "mapping_status": "mapped",
        "energy_provider": provider,
        "production_quality_status": "complete",
        "production_source": "monthly",
        "availability_pct": "99",
        "invoice_status": "confirmed",
    }


def test_fusionsolar_remains_usable_without_sigenergy_history() -> None:
    payload = {
        **base_payload("FusionSolar"),
        "sigenergy_history_status": "missing_permission",
        "sigenergy_energy_unit_confirmed": False,
    }
    assert evaluate_report_quality(payload, scope="individual").status == READY


def test_sigenergy_history_permission_and_unit_block_monthly_close() -> None:
    payload = {
        **base_payload("Sigenergy"),
        "sigenergy_history_status": "forbidden",
        "sigenergy_energy_unit_confirmed": False,
    }
    result = evaluate_report_quality(payload, scope="individual")
    assert result.status == BLOCKED
    assert {
        "sigenergy_history_forbidden",
        "sigenergy_energy_unit_unconfirmed",
    } <= {item.code for item in result.blockers}
    assert any("Expertcom" in item.remediation for item in result.blockers)
