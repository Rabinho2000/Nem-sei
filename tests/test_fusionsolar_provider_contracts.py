from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import app as app_module
from monitoring_board.services.fusionsolar import map_fusionsolar_status


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fusionsolar"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeSession:
    def __init__(self, payloads_by_url: dict[str, dict[str, Any]]) -> None:
        self.payloads_by_url = payloads_by_url
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json: dict[str, Any], timeout: int) -> FakeResponse:
        self.posts.append((url, json))
        assert timeout == 30
        return FakeResponse(self.payloads_by_url[url])


def fixture_station_codes(stations: list[dict[str, Any]]) -> list[str]:
    return [
        str(row.get("plantCode") or row.get("stationCode") or "").strip()
        for row in stations
    ]


def fixture_realtime_map() -> dict[str, dict[str, Any]]:
    return {
        str(row["stationCode"]): row
        for row in load_fixture("realtime_kpi.json")["data"]
    }


def fixture_alarm_map() -> dict[str, list[dict[str, Any]]]:
    alarm_map: dict[str, list[dict[str, Any]]] = {}
    for row in load_fixture("alarms_active.json")["data"]:
        alarm_map.setdefault(str(row["stationCode"]), []).append(row)
    return alarm_map


def test_fetch_helpers_parse_fusionsolar_fixture_shapes() -> None:
    session = FakeSession(
        {
            "https://fusion.test/thirdData/stations": load_fixture("stations_page_1.json"),
            "https://fusion.test/thirdData/getStationRealKpi": load_fixture("realtime_kpi.json"),
            "https://fusion.test/thirdData/getAlarmList": load_fixture("alarms_active.json"),
        }
    )

    stations = app_module.fetch_fusionsolar_stations(
        session,
        base_url="https://fusion.test",
        endpoint="/thirdData/stations",
    )
    station_codes = fixture_station_codes(stations)
    realtime = app_module.fetch_fusionsolar_realtime_map(
        session,
        base_url="https://fusion.test",
        endpoint="/thirdData/getStationRealKpi",
        station_codes=station_codes,
    )
    alarms = app_module.fetch_fusionsolar_alarm_map(
        session,
        base_url="https://fusion.test",
        endpoint="/thirdData/getAlarmList",
        station_codes=station_codes,
    )

    day_rows = app_module.normalize_fusionsolar_kpi_rows(load_fixture("kpi_day_rows.json")["data"])
    month_rows = app_module.normalize_fusionsolar_kpi_rows(load_fixture("kpi_month_rows.json")["data"])

    assert station_codes == ["FS-PLANT-001", "FS-PLANT-002", "FS-PLANT-003", "FS-STATION-004"]
    assert realtime["FS-PLANT-001"]["dataItemMap"]["real_health_state"] == "3"
    assert alarms["FS-PLANT-002"][0]["lev"] == 3
    assert day_rows[0]["dataItemMap"]["PVYield"] == "123.45"
    assert month_rows[0]["dataItemMap"]["PVYield"] == "3456.78"
    assert session.posts[0][1] == {"pageNo": 1}
    assert session.posts[1][1]["stationCodes"] == ",".join(station_codes)


def test_normalize_fusionsolar_fixture_rows_maps_statuses_and_alarm_severity() -> None:
    stations = {
        code: row
        for row in load_fixture("stations_page_1.json")["data"]["list"]
        for code in [str(row.get("plantCode") or row.get("stationCode"))]
    }
    realtime = fixture_realtime_map()
    alarms = fixture_alarm_map()

    healthy = app_module.normalize_fusionsolar_plant_row(
        stations["FS-PLANT-001"],
        realtime["FS-PLANT-001"],
        [],
    )
    minor_alarm = app_module.normalize_fusionsolar_plant_row(
        stations["FS-PLANT-002"],
        realtime["FS-PLANT-002"],
        alarms["FS-PLANT-002"],
    )
    critical_alarm = app_module.normalize_fusionsolar_plant_row(
        stations["FS-PLANT-003"],
        realtime["FS-PLANT-003"],
        alarms["FS-PLANT-003"],
    )
    disconnected = app_module.normalize_fusionsolar_plant_row(
        stations["FS-STATION-004"],
        realtime["FS-STATION-004"],
        [],
    )

    assert map_fusionsolar_status("3") == "Operacional"
    assert map_fusionsolar_status("2") == "Erro"
    assert map_fusionsolar_status("1") == "Desconectada"
    assert healthy["status"] == "Operacional"
    assert minor_alarm["status"] == "Alerta"
    assert minor_alarm["alarm_levels"] == "3"
    assert critical_alarm["status"] == "Erro"
    assert critical_alarm["alarm_levels"] == "1"
    assert disconnected["status"] == "Desconectada"
    assert disconnected["external_id"] == "FS-STATION-004"


def test_parse_fusionsolar_collect_date_documents_fixture_timestamp_behavior() -> None:
    day_rows = app_module.normalize_fusionsolar_kpi_rows(load_fixture("kpi_day_rows.json")["data"])
    month_rows = app_module.normalize_fusionsolar_kpi_rows(load_fixture("kpi_month_rows.json")["data"])

    assert app_module.parse_fusionsolar_collect_date(day_rows[0]) == date(2026, 1, 15)
    assert app_module.parse_fusionsolar_collect_date(month_rows[0]) == date(2026, 1, 1)
    assert app_module.parse_fusionsolar_collect_date({"collectTime": "2026-01-16"}) == date(2026, 1, 16)
    assert app_module.parse_fusionsolar_collect_date({"collectTime": "bad", "date": "15/01/2026"}) == date(2026, 1, 15)
    assert app_module.parse_fusionsolar_collect_date({}, fallback_date=date(2026, 1, 31)) == date(2026, 1, 31)


def test_select_production_value_prioritizes_fixture_kwh_sources_and_raw_values() -> None:
    day_rows = app_module.normalize_fusionsolar_kpi_rows(load_fixture("kpi_day_rows.json")["data"])
    month_rows = app_module.normalize_fusionsolar_kpi_rows(load_fixture("kpi_month_rows.json")["data"])

    assert app_module.select_production_value(day_rows[0]["dataItemMap"]) == (123.45, "PVYield", "123.45")
    assert app_module.select_production_value(day_rows[1]["dataItemMap"]) == (98.76, "inverterYield", "98.76")
    assert app_module.select_production_value(day_rows[2]["dataItemMap"]) == (54.32, "inverter_power", "54.32")
    assert app_module.select_production_value(month_rows[0]["dataItemMap"]) == (3456.78, "PVYield", "3456.78")
    assert app_module.select_production_value(month_rows[1]["dataItemMap"]) == (2100.5, "inverterYield", "2100.50")
    assert app_module.select_production_value(month_rows[2]["dataItemMap"]) == (1500.25, "inverter_power", "1500.25")


@pytest.mark.parametrize(
    ("payload", "recognizer"),
    [
        ({"success": False, "failCode": 407, "message": "API call limit exceeded"}, app_module.is_fusionsolar_rate_limit_error),
        ({"success": False, "failCode": "407", "message": "API call limit exceeded"}, app_module.is_fusionsolar_rate_limit_error),
        ({"success": False, "failCode": 305, "message": "USER_MUST_RELOGIN"}, app_module.is_fusionsolar_session_expired_error),
        ({"success": False, "failCode": "305", "message": "USER_MUST_RELOGIN"}, app_module.is_fusionsolar_session_expired_error),
    ],
)
def test_post_fusionsolar_json_error_payloads_feed_existing_error_recognizers(
    payload: dict[str, Any],
    recognizer: Any,
) -> None:
    session = FakeSession({"https://fusion.test/thirdData/fail": payload})

    with pytest.raises(ValueError) as exc_info:
        app_module.post_fusionsolar_json(
            session,
            "https://fusion.test/thirdData/fail",
            {"stationCodes": "FS-PLANT-001"},
            expected_message="FusionSolar fixture failure",
        )

    assert recognizer(exc_info.value)
