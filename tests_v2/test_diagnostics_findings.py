"""Pure, DB-free tests for the deterministic operational findings rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from nemsei.diagnostics.findings import DiagnosticFinding, evaluate_asset_findings
from nemsei.monitoring.production_window import ProductionWindow


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def row(
    device_id: int,
    label: str,
    *,
    device_kind: str = "inverter",
    has_reading: bool = True,
    availability_status: str = "available",
    active_power_kw=None,
    day_energy_kwh=None,
    observed_at: datetime | None = NOW,
) -> dict:
    return {
        "device_id": device_id,
        "label": label,
        "device_kind": device_kind,
        "model": None,
        "rated_power_kw": None,
        "observed_at": observed_at,
        "availability_status": availability_status,
        "active_power_kw": Decimal(str(active_power_kw)) if active_power_kw is not None else None,
        "day_energy_kwh": Decimal(str(day_energy_kwh)) if day_energy_kwh is not None else None,
        "freshness": "unknown",
        "source_kind": "live_read",
        "has_reading": has_reading,
    }


def by_rule(findings: list[DiagnosticFinding], rule_code: str) -> list[DiagnosticFinding]:
    return [finding for finding in findings if finding.rule_code == rule_code]


def test_a_healthy_pair_of_comparable_inverters_raises_nothing() -> None:
    rows = [
        row(1, "INV-1", active_power_kw="10.0", day_energy_kwh="20.0"),
        row(2, "INV-2", active_power_kw="9.5", day_energy_kwh="19.0"),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert findings == []


def test_a_device_with_no_reading_at_all_is_flagged_and_short_circuits_other_state_rules() -> None:
    rows = [row(1, "INV-1", has_reading=False, availability_status="unknown", observed_at=None)]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert [finding.rule_code for finding in findings] == ["device_no_history"]
    assert findings[0].severity == "warning"
    assert findings[0].device_id == 1
    assert findings[0].missing_data is not None


def test_an_unavailable_device_is_critical_and_reports_the_evidence() -> None:
    rows = [row(1, "INV-1", availability_status="unavailable", active_power_kw="0")]
    findings = evaluate_asset_findings(rows, asset_id=7, now=NOW)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_code == "device_unavailable"
    assert finding.severity == "critical"
    assert finding.asset_id == 7
    assert finding.evidence["availability_status"] == "unavailable"
    assert finding.still_active is True


def test_active_since_walks_history_until_the_condition_breaks() -> None:
    history = [
        SimpleNamespace(availability_status="unavailable", observed_at=NOW),
        SimpleNamespace(availability_status="unavailable", observed_at=NOW - timedelta(hours=1)),
        SimpleNamespace(availability_status="unavailable", observed_at=NOW - timedelta(hours=2)),
        SimpleNamespace(availability_status="available", observed_at=NOW - timedelta(hours=3)),
    ]
    rows = [row(1, "INV-1", availability_status="unavailable")]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW, history_for_device=lambda device_id: history)
    assert findings[0].active_since == NOW - timedelta(hours=2)
    assert findings[0].missing_data is None


def test_without_history_active_since_falls_back_to_the_latest_observation() -> None:
    rows = [row(1, "INV-1", availability_status="unavailable")]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert findings[0].active_since == NOW
    assert findings[0].missing_data is not None


def test_unknown_status_is_a_distinct_finding_from_no_history() -> None:
    rows = [row(1, "INV-1", availability_status="unknown")]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert [finding.rule_code for finding in findings] == ["device_unknown_status"]
    assert findings[0].severity == "warning"


def test_a_reading_older_than_the_stale_threshold_is_flagged() -> None:
    rows = [row(1, "INV-1", observed_at=NOW - timedelta(hours=30))]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert [finding.rule_code for finding in findings] == ["stale_reading"]
    assert findings[0].evidence["age_hours"] == 30.0


def test_a_reading_inside_the_stale_threshold_is_not_flagged() -> None:
    rows = [row(1, "INV-1", observed_at=NOW - timedelta(hours=2))]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert findings == []


def test_zero_power_while_a_comparable_peer_produces_is_critical() -> None:
    rows = [
        row(1, "INV-1", active_power_kw="0"),
        row(2, "INV-2", active_power_kw="12.5"),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    zero = by_rule(findings, "zero_power_while_peers_active")
    assert len(zero) == 1
    assert zero[0].device_id == 1
    assert zero[0].evidence["peer_max_active_power_kw"] == "12.5"
    # The healthy producing device does not also get flagged against itself.
    assert by_rule(findings, "power_disparity_among_peers") == []


def test_both_near_zero_peers_are_not_flagged_as_a_disparity() -> None:
    """Below the noise floor, "0.02kW vs 0.05kW" is not a real disparity."""
    rows = [
        row(1, "INV-1", active_power_kw="0.02"),
        row(2, "INV-2", active_power_kw="0.05"),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert findings == []


def test_a_partial_power_shortfall_is_a_warning_not_critical() -> None:
    rows = [
        row(1, "INV-1", active_power_kw="4.0"),
        row(2, "INV-2", active_power_kw="12.0"),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    disparity = by_rule(findings, "power_disparity_among_peers")
    assert len(disparity) == 1
    assert disparity[0].severity == "warning"
    assert disparity[0].device_id == 1


def test_daily_energy_disparity_is_flagged_independently_of_instantaneous_power() -> None:
    rows = [
        row(1, "INV-1", active_power_kw="10.0", day_energy_kwh="5.0"),
        row(2, "INV-2", active_power_kw="10.5", day_energy_kwh="30.0"),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    energy = by_rule(findings, "daily_energy_disparity_among_peers")
    assert len(energy) == 1
    assert energy[0].device_id == 1
    # Power itself was comparable, so no power-disparity finding fires too.
    assert by_rule(findings, "power_disparity_among_peers") == []


def test_readings_too_far_apart_in_time_are_not_compared_to_each_other() -> None:
    """A stale device must not also look like a "disparity" against a fresh one --
    that is a different, separately-flagged problem."""
    rows = [
        row(1, "INV-1", active_power_kw="0", observed_at=NOW - timedelta(hours=30)),
        row(2, "INV-2", active_power_kw="12.0", observed_at=NOW),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert by_rule(findings, "zero_power_while_peers_active") == []
    assert by_rule(findings, "power_disparity_among_peers") == []
    # But device 1's own staleness is still caught.
    assert by_rule(findings, "stale_reading")[0].device_id == 1


def test_different_device_kinds_are_never_compared_to_each_other() -> None:
    rows = [
        row(1, "INV-1", device_kind="inverter", active_power_kw="0"),
        row(2, "METER-1", device_kind="meter", active_power_kw="12.0"),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert by_rule(findings, "zero_power_while_peers_active") == []


def test_partial_device_coverage_is_an_asset_level_finding() -> None:
    rows = [
        row(1, "INV-1", active_power_kw="10.0"),
        row(2, "INV-2", has_reading=False, observed_at=None),
    ]
    findings = evaluate_asset_findings(rows, asset_id=3, now=NOW)
    coverage = by_rule(findings, "partial_device_coverage")
    assert len(coverage) == 1
    assert coverage[0].device_id is None
    assert coverage[0].asset_id == 3
    assert coverage[0].evidence == {"devices_with_reading": 1, "devices_total": 2}


def test_full_coverage_does_not_raise_the_partial_coverage_finding() -> None:
    rows = [row(1, "INV-1", active_power_kw="10.0"), row(2, "INV-2", active_power_kw="9.0")]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert by_rule(findings, "partial_device_coverage") == []


def test_zero_coverage_does_not_double_report_alongside_per_device_no_history() -> None:
    """Every device missing is already fully explained by N device_no_history
    findings; a redundant asset-level "0 of N" finding would just be noise."""
    rows = [row(1, "INV-1", has_reading=False, observed_at=None), row(2, "INV-2", has_reading=False, observed_at=None)]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert by_rule(findings, "partial_device_coverage") == []
    assert len(by_rule(findings, "device_no_history")) == 2


def test_findings_are_ordered_worst_first() -> None:
    rows = [
        row(1, "INV-1", availability_status="unknown"),
        row(2, "INV-2", availability_status="unavailable"),
        row(3, "INV-3", has_reading=False, observed_at=None),
    ]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    # device_unavailable (critical), device_unknown_status + device_no_history
    # (warning, alphabetical by label within the tier), partial_device_coverage
    # (info, asset-level) last.
    assert [finding.severity for finding in findings] == ["critical", "warning", "warning", "info"]


# --- production window: total production failure ----------------------


PRODUCTIVE = ProductionWindow(state="productive")
DARK = ProductionWindow(state="dark")
UNKNOWN = ProductionWindow(state="unknown")


def test_zero_power_at_night_is_not_a_finding() -> None:
    """The whole reason `production_window` exists: nothing generated at
    night is not a fault."""
    rows = [row(1, "INV-1", active_power_kw=0)]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW, production_window=DARK)
    assert by_rule(findings, "zero_production_in_productive_window") == []


def test_zero_power_with_no_window_given_is_not_a_finding() -> None:
    """The default: every caller that predates coordinates existing keeps
    working exactly as before, silently, rather than needing to opt out."""
    rows = [row(1, "INV-1", active_power_kw=0)]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW)
    assert by_rule(findings, "zero_production_in_productive_window") == []


def test_zero_power_with_an_unknown_window_is_not_a_finding() -> None:
    """No coordinates recorded -- `window.is_productive` is `False` for
    `unknown` too, and guessing would invent a claim about a location
    nobody located."""
    rows = [row(1, "INV-1", active_power_kw=0)]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW, production_window=UNKNOWN)
    assert by_rule(findings, "zero_production_in_productive_window") == []


def test_zero_power_during_the_day_is_not_immediately_a_finding() -> None:
    """A cloud passing over is not a total production failure -- it needs to
    have held for `_PRODUCTION_ABSENCE_THRESHOLD` (30 min), by real evidence,
    exactly like every other duration-gated rule in this module."""
    history = [SimpleNamespace(active_power_kw=Decimal("0"), observed_at=NOW)]
    rows = [row(1, "INV-1", active_power_kw=0)]
    findings = evaluate_asset_findings(
        rows, asset_id=1, now=NOW, production_window=PRODUCTIVE, history_for_device=lambda device_id: history
    )
    assert by_rule(findings, "zero_production_in_productive_window") == []


def test_zero_power_for_over_thirty_minutes_in_daylight_is_critical() -> None:
    history = [
        SimpleNamespace(active_power_kw=Decimal("0"), observed_at=NOW),
        SimpleNamespace(active_power_kw=Decimal("0"), observed_at=NOW - timedelta(minutes=20)),
        SimpleNamespace(active_power_kw=Decimal("0"), observed_at=NOW - timedelta(minutes=35)),
        SimpleNamespace(active_power_kw=Decimal("4.2"), observed_at=NOW - timedelta(minutes=50)),
    ]
    rows = [row(1, "INV-1", active_power_kw=0)]
    findings = evaluate_asset_findings(
        rows, asset_id=1, now=NOW, production_window=PRODUCTIVE, history_for_device=lambda device_id: history
    )
    matches = by_rule(findings, "zero_production_in_productive_window")
    assert len(matches) == 1
    assert matches[0].severity == "critical"
    assert matches[0].active_since == NOW - timedelta(minutes=35)


def test_any_positive_power_reading_clears_the_finding() -> None:
    """One inverter still producing is the opposite of a total failure."""
    rows = [row(1, "INV-1", active_power_kw=0), row(2, "INV-2", active_power_kw=3.1)]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW, production_window=PRODUCTIVE)
    assert by_rule(findings, "zero_production_in_productive_window") == []


def test_no_reading_at_all_is_a_different_problem_not_zero_production() -> None:
    """Silence is not evidence of zero -- `device_no_history` already covers
    it, and conflating the two would claim a reading that does not exist."""
    rows = [row(1, "INV-1", has_reading=False, observed_at=None)]
    findings = evaluate_asset_findings(rows, asset_id=1, now=NOW, production_window=PRODUCTIVE)
    assert by_rule(findings, "zero_production_in_productive_window") == []
    assert by_rule(findings, "device_no_history") != []


def test_the_joint_zero_since_is_the_latest_device_to_stop_not_the_earliest() -> None:
    """Device A has read zero for an hour; Device B only stopped 10 minutes
    ago. The asset was not "fully at zero" until B also dropped -- 10 minutes
    ago, not an hour ago."""
    history_a = [SimpleNamespace(active_power_kw=Decimal("0"), observed_at=NOW - timedelta(minutes=t)) for t in (0, 30, 60)]
    history_b = [
        SimpleNamespace(active_power_kw=Decimal("0"), observed_at=NOW),
        SimpleNamespace(active_power_kw=Decimal("3.0"), observed_at=NOW - timedelta(minutes=10)),
    ]
    histories = {1: history_a, 2: history_b}
    rows = [row(1, "INV-1", active_power_kw=0), row(2, "INV-2", active_power_kw=0)]
    findings = evaluate_asset_findings(
        rows, asset_id=1, now=NOW, production_window=PRODUCTIVE, history_for_device=lambda device_id: histories[device_id]
    )
    # Joint zero only since 10 minutes ago -- short of the 30-minute
    # threshold, so this must not fire yet even though device A alone would.
    assert by_rule(findings, "zero_production_in_productive_window") == []
