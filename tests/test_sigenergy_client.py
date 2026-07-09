from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from monitoring_board.services.api_rate_limit import ApiRateLimitError
from monitoring_board.services.sigenergy_client import SigenergyClient
from monitoring_board.services.sigenergy_errors import SigenergyApiError
from monitoring_board.services.sigenergy_models import (
    SigenergyCredentials,
    SigenergyEndpoints,
    normalize_energy_flow,
    normalize_system,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sigenergy"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def endpoints() -> SigenergyEndpoints:
    return SigenergyEndpoints(
        base_url="https://sigenergy.example.test",
        login_endpoint="/openapi/auth/login/key",
        systems_endpoint="/openapi/system",
        energy_flow_endpoint="/openapi/systems/{system_id}/energyFlow",
        region="eu",
    )


class FakeResponse:
    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            exc = requests.HTTPError(f"{self.status_code} error")
            exc.response = self  # type: ignore[assignment]
            raise exc

    def json(self) -> Any:
        return self.payload


class QueueSession:
    def __init__(self, responses_by_url: dict[str, list[FakeResponse]]) -> None:
        self.responses_by_url = responses_by_url
        self.posts: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: int) -> FakeResponse:
        assert timeout == 30
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.responses_by_url[url].pop(0)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: Any | None,
        timeout: int,
    ) -> FakeResponse:
        assert timeout == 30
        self.requests.append({"method": method, "url": url, "json": json, "headers": headers})
        return self.responses_by_url[url].pop(0)


def client(session: QueueSession, *, system_ids: str = "", token_cache: dict[str, dict[str, Any]] | None = None) -> SigenergyClient:
    return SigenergyClient(
        endpoints(),
        SigenergyCredentials("fixture-app-key", "fixture-app-secret"),
        system_ids=system_ids,
        session=session,
        token_cache=token_cache if token_cache is not None else {},
        sleeper=lambda _seconds: None,
    )


def test_login_extracts_token_and_sends_region_header() -> None:
    session = QueueSession(
        {
            "https://sigenergy.example.test/openapi/auth/login/key": [FakeResponse(load_fixture("auth_success_object.json"))]
        }
    )

    token = client(session).get_access_token(force_login=True)

    assert token == "fake-sigenergy-token-object"
    assert session.posts[0]["url"] == "https://sigenergy.example.test/openapi/auth/login/key"
    assert session.posts[0]["headers"]["sigen-region"] == "eu"
    assert session.posts[0]["json"]["key"] != "fixture-app-secret"


def test_login_accepts_data_as_json_string() -> None:
    session = QueueSession(
        {
            "https://sigenergy.example.test/openapi/auth/login/key": [FakeResponse(load_fixture("auth_success_json_string.json"))]
        }
    )

    assert client(session).get_access_token(force_login=True) == "fake-sigenergy-token-json-string"


def test_list_systems_uses_api_rows() -> None:
    session = QueueSession(
        {
            "https://sigenergy.example.test/openapi/auth/login/key": [FakeResponse(load_fixture("auth_success_object.json"))],
            "https://sigenergy.example.test/openapi/system": [FakeResponse(load_fixture("systems_list.json"))],
        }
    )

    systems = client(session).list_systems()

    assert [row["systemId"] for row in systems] == ["SIG-001", "SIG-002"]
    assert session.requests[0]["headers"]["Authorization"] == "Bearer fake-sigenergy-token-object"
    assert session.requests[0]["headers"]["sigen-region"] == "eu"


def test_list_systems_falls_back_to_configured_system_ids_without_api_call() -> None:
    session = QueueSession({})

    systems = client(session, system_ids="SIG-001, SIG-002").list_systems()

    assert systems == [
        {"systemId": "SIG-001", "systemName": "SIG-001"},
        {"systemId": "SIG-002", "systemName": "SIG-002"},
    ]
    assert session.posts == []
    assert session.requests == []


def test_energy_flow_current_by_system() -> None:
    session = QueueSession(
        {
            "https://sigenergy.example.test/openapi/auth/login/key": [FakeResponse(load_fixture("auth_success_object.json"))],
            "https://sigenergy.example.test/openapi/systems/SIG-001/energyFlow": [FakeResponse(load_fixture("energy_flow.json"))],
        }
    )

    flow = client(session).get_energy_flow("SIG-001")

    assert flow["systemId"] == "SIG-001"
    assert flow["pvPower"] == 4.25
    assert session.requests[0]["url"] == "https://sigenergy.example.test/openapi/systems/SIG-001/energyFlow"


def test_401_invalidates_token_and_relogs_once() -> None:
    session = QueueSession(
        {
            "https://sigenergy.example.test/openapi/auth/login/key": [
                FakeResponse(load_fixture("auth_success_object.json")),
                FakeResponse({"code": 0, "data": {"accessToken": "second-token", "expiresIn": 600}}),
            ],
            "https://sigenergy.example.test/openapi/systems/SIG-001/energyFlow": [
                FakeResponse({"code": 401, "msg": "expired"}, status_code=401),
                FakeResponse(load_fixture("energy_flow.json")),
            ],
        }
    )

    flow = client(session).get_energy_flow("SIG-001")

    assert flow["systemId"] == "SIG-001"
    assert len(session.posts) == 2
    assert session.requests[0]["headers"]["Authorization"] == "Bearer fake-sigenergy-token-object"
    assert session.requests[1]["headers"]["Authorization"] == "Bearer second-token"


def test_http_429_raises_common_rate_limit_error() -> None:
    session = QueueSession(
        {
            "https://sigenergy.example.test/openapi/auth/login/key": [FakeResponse(load_fixture("auth_success_object.json"))],
            "https://sigenergy.example.test/openapi/systems/SIG-001/energyFlow": [
                FakeResponse({"code": 429, "msg": "too many"}, status_code=429)
            ],
        }
    )

    with pytest.raises(ApiRateLimitError, match="Sigenergy HTTP 429"):
        client(session).get_energy_flow("SIG-001")


def test_normalizers_tolerate_missing_or_unexpected_fields() -> None:
    system = normalize_system({"systemId": "SIG-001"})
    flow = normalize_energy_flow({"unexpected": "shape"})

    assert system["external_id"] == "SIG-001"
    assert system["normalized_status"] == "Sem dados"
    assert flow["pv_power_kw"] is None
    assert flow["payload"] == {"unexpected": "shape"}
    with pytest.raises(SigenergyApiError):
        normalize_system({"name": "missing id"})
