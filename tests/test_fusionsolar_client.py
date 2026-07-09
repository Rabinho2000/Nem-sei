from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import requests

from monitoring_board.services.fusionsolar_client import FusionSolarClient, extract_xsrf_token
from monitoring_board.services.fusionsolar_errors import (
    FusionSolarApiError,
    FusionSolarRateLimitError,
)
from monitoring_board.services.fusionsolar_models import FusionSolarCredentials, FusionSolarEndpoints


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fusionsolar"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def endpoints() -> FusionSolarEndpoints:
    return FusionSolarEndpoints(
        base_url="https://fusion.test",
        login_endpoint="/thirdData/login",
        plants_endpoint="/thirdData/stations",
        real_time_endpoint="/thirdData/getStationRealKpi",
        device_list_endpoint="/thirdData/getDevList",
        device_real_time_endpoint="/thirdData/getDevRealKpi",
        device_history_endpoint="/thirdData/getDevHistoryKpi",
        alarms_endpoint="/thirdData/getAlarmList",
        day_kpi_endpoint="/thirdData/getKpiStationDay",
        month_kpi_endpoint="/thirdData/getKpiStationMonth",
    )


class FakeCookieJar(dict[str, str]):
    def get(self, name: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return super().get(name, default)


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self.payload = payload
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self  # type: ignore[assignment]
            raise error

    def json(self) -> dict[str, Any]:
        return self.payload


class QueueSession:
    def __init__(self, responses_by_url: dict[str, list[FakeResponse]]) -> None:
        self.responses_by_url = responses_by_url
        self.headers: dict[str, str] = {}
        self.cookies: FakeCookieJar = FakeCookieJar()
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: int,
    ) -> FakeResponse:
        assert timeout == 30
        self.posts.append((url, json))
        responses = self.responses_by_url[url]
        return responses.pop(0)


def client_with_session(session: QueueSession, *, allow_sleep: bool = False) -> FusionSolarClient:
    return FusionSolarClient(
        endpoints(),
        FusionSolarCredentials("fixture-user", "fixture-secret"),
        session_factory=lambda: session,
        session_cache={},
        allow_sleep=allow_sleep,
        sleeper=lambda _seconds: None,
    )


def test_extracts_xsrf_token_from_header_and_cookie() -> None:
    header_response = FakeResponse(load_fixture("login_success.json"), headers={"xsrf-token": " header-token "})
    cookie_session = QueueSession({})
    cookie_session.cookies["XSRF-TOKEN"] = "cookie-token"

    assert extract_xsrf_token(header_response, QueueSession({})) == "header-token"
    assert extract_xsrf_token(FakeResponse(load_fixture("login_success.json")), cookie_session) == "cookie-token"


def test_login_caches_session_and_does_not_log_secret(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="monitoring_board.services.fusionsolar_client")
    session = QueueSession(
        {
            "https://fusion.test/thirdData/login": [
                FakeResponse(load_fixture("login_success.json"), headers={"XSRF-TOKEN": "token-1"})
            ]
        }
    )
    client = client_with_session(session)

    first_session, first_token = client.login()
    second_session, second_token = client.login()

    assert first_session is second_session is session
    assert first_token == second_token == "token-1"
    assert session.headers["XSRF-TOKEN"] == "token-1"
    assert len(session.posts) == 1
    assert "fixture-secret" not in caplog.text
    assert "token-1" not in caplog.text


def test_post_json_validates_success_fail_code_and_required_data() -> None:
    session = QueueSession(
        {
            "https://fusion.test/thirdData/getStationRealKpi": [
                FakeResponse({"success": False, "failCode": 100, "message": "Bad request"}),
                FakeResponse({"success": True, "failCode": 0, "message": "OK"}),
            ]
        }
    )
    client = client_with_session(session)

    with pytest.raises(FusionSolarApiError, match="Bad request"):
        client.post_json(
            session,
            "/thirdData/getStationRealKpi",
            {"stationCodes": "FS-PLANT-001"},
            expected_message="Falha realtime",
            require_data=True,
        )
    with pytest.raises(FusionSolarApiError, match="nao trouxe data"):
        client.post_json(
            session,
            "/thirdData/getStationRealKpi",
            {"stationCodes": "FS-PLANT-001"},
            expected_message="Falha realtime",
            require_data=True,
        )


def test_stations_uses_pagination() -> None:
    page_1 = load_fixture("stations_page_1.json")
    page_1["data"]["pageCount"] = 2
    page_1["data"]["list"] = page_1["data"]["list"][:1]
    session = QueueSession(
        {
            "https://fusion.test/thirdData/login": [
                FakeResponse(load_fixture("login_success.json"), headers={"XSRF-TOKEN": "token"})
            ],
            "https://fusion.test/thirdData/stations": [
                FakeResponse(page_1),
                FakeResponse(load_fixture("stations_page_2.json")),
            ],
        }
    )
    client = client_with_session(session)

    stations = client.stations()

    assert [row.get("plantCode") for row in stations] == ["FS-PLANT-001", "FS-PLANT-002"]
    assert session.posts[1:] == [
        ("https://fusion.test/thirdData/stations", {"pageNo": 1}),
        ("https://fusion.test/thirdData/stations", {"pageNo": 2}),
    ]


def test_fail_code_407_raises_rate_limit() -> None:
    session = QueueSession(
        {
            "https://fusion.test/thirdData/getStationRealKpi": [
                FakeResponse({"success": False, "failCode": 407, "message": "Call frequency limit"})
            ]
        }
    )
    client = client_with_session(session)

    with pytest.raises(FusionSolarRateLimitError):
        client.post_json(
            session,
            "/thirdData/getStationRealKpi",
            {"stationCodes": "FS-PLANT-001"},
            expected_message="Falha realtime",
            require_data=True,
        )


def test_http_429_raises_rate_limit_without_retrying() -> None:
    session = QueueSession(
        {
            "https://fusion.test/thirdData/getStationRealKpi": [
                FakeResponse({"message": "Too many requests"}, status_code=429)
            ]
        }
    )
    client = client_with_session(session)

    with pytest.raises(FusionSolarRateLimitError):
        client.post_json(
            session,
            "/thirdData/getStationRealKpi",
            {"stationCodes": "FS-PLANT-001"},
            expected_message="Falha realtime",
            require_data=True,
        )
    assert len(session.posts) == 1


def test_session_expired_305_relogs_once() -> None:
    first_session = QueueSession(
        {
            "https://fusion.test/thirdData/login": [
                FakeResponse(load_fixture("login_success.json"), headers={"XSRF-TOKEN": "token-1"})
            ],
            "https://fusion.test/thirdData/getStationRealKpi": [
                FakeResponse({"success": False, "failCode": 305, "message": "USER_MUST_RELOGIN"})
            ],
        }
    )
    second_session = QueueSession(
        {
            "https://fusion.test/thirdData/login": [
                FakeResponse(load_fixture("login_success.json"), headers={"XSRF-TOKEN": "token-2"})
            ],
            "https://fusion.test/thirdData/getStationRealKpi": [FakeResponse(load_fixture("realtime_kpi.json"))],
        }
    )
    sessions = [first_session, second_session]
    client = FusionSolarClient(
        endpoints(),
        FusionSolarCredentials("fixture-user", "fixture-secret"),
        session_factory=lambda: sessions.pop(0),
        session_cache={},
        sleeper=lambda _seconds: None,
    )

    result = client.station_realtime_kpi(["FS-PLANT-001"])

    assert result["FS-PLANT-001"]["dataItemMap"]["active_power"] == "34.2"
    assert first_session.headers["XSRF-TOKEN"] == "token-1"
    assert second_session.headers["XSRF-TOKEN"] == "token-2"
