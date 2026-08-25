"""Raw, read-only Sigenergy OpenAPI HTTP boundary.

Provider response structures stay inside this package.  The client never
opens a database transaction and emits only normalized :class:`ProviderError`
values to its orchestration layer.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from nemsei.providers.errors import ProviderError, ProviderErrorCode


@dataclass(frozen=True)
class SigenergyCredentials:
    app_key: str
    app_secret: str


@dataclass(frozen=True)
class SigenergyEndpoints:
    base_url: str
    auth_endpoint: str
    systems_endpoint: str
    energy_flow_endpoint: str
    region: str
    # Daily history. Defaulted rather than required so an existing connection
    # configured before production history existed keeps working unchanged.
    history_endpoint: str = "/openapi/systems/{system_id}/history"


@dataclass(frozen=True)
class SigenergyHttpResponse:
    status_code: int
    headers: dict[str, str]
    payload: dict[str, Any]


class SigenergyTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str],
        json_payload: dict[str, Any] | None,
        timeout_seconds: int,
    ) -> SigenergyHttpResponse: ...


class SigenergyClientError(Exception):
    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.safe_message)
        self.error = error


class UrllibSigenergyTransport:
    """Small stdlib transport used by the runtime; tests inject a fake."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str],
        json_payload: dict[str, Any] | None,
        timeout_seconds: int,
    ) -> SigenergyHttpResponse:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        body = json.dumps(json_payload).encode("utf-8") if json_payload is not None else None
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return self._response(response.status, dict(response.headers.items()), response.read())
        except HTTPError as exc:
            payload = self._response(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()).payload
            return SigenergyHttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, payload)
        except TimeoutError as exc:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.TIMEOUT, "Sigenergy request timed out.", transient=True)) from exc
        except URLError as exc:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.UNAVAILABLE, "Sigenergy is unreachable.", transient=True)) from exc

    @staticmethod
    def _response(status: int, headers: dict[str, str], body: bytes) -> SigenergyHttpResponse:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy returned invalid JSON.")) from exc
        if not isinstance(payload, dict):
            raise SigenergyClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy returned an unexpected response."))
        return SigenergyHttpResponse(status, headers, payload)


class SigenergyClient:
    def __init__(
        self,
        endpoints: SigenergyEndpoints,
        credentials: SigenergyCredentials,
        *,
        transport: SigenergyTransport | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.endpoints = endpoints
        self.credentials = credentials
        self.transport = transport or UrllibSigenergyTransport()
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None

    def authenticate(self) -> None:
        key = f"{self.credentials.app_key.strip()}:{self.credentials.app_secret.strip()}"
        if not self.credentials.app_key.strip() or not self.credentials.app_secret.strip():
            raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy credentials are not configured."))
        response = self._request(
            "POST",
            self.endpoints.auth_endpoint,
            json_payload={"key": base64.b64encode(key.encode("utf-8")).decode("ascii")},
            authenticated=False,
        )
        data = _response_data(response.payload)
        if not isinstance(data, dict):
            raise SigenergyClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy authentication response is invalid."))
        token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        if not token:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "Sigenergy authentication did not return an access token."))
        self._access_token = token

    def discover_systems(self) -> list[dict[str, Any]]:
        data = _response_data(self._request("GET", self.endpoints.systems_endpoint).payload)
        rows = _rows_from_data(data)
        if not rows and data not in ([], {}):
            raise SigenergyClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy discovery response has no system list."))
        return rows

    def get_energy_flow(self, system_id: str) -> dict[str, Any]:
        identifier = system_id.strip()
        if not identifier:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy system identifier is required."))
        endpoint = self.endpoints.energy_flow_endpoint.replace("{systemId}", identifier).replace("{system_id}", identifier)
        data = _response_data(self._request("GET", endpoint).payload)
        if not isinstance(data, dict):
            raise SigenergyClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy energy-flow response is invalid."))
        return data

    def get_system_history(self, system_id: str, *, target_date: date, level: str = "Day") -> dict[str, Any]:
        """One day of cumulative energy counters for one system.

        The contract is V1's, derived from its working implementation rather
        than from documentation: `GET .../history?level=Day&date=YYYY-MM-DD`.
        What the provider means by that date is *not* settled here -- the
        caller supplies a day already resolved in an operator-verified source
        timezone, the same discipline FusionSolar's daily read follows.
        """
        identifier = system_id.strip()
        if not identifier:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy system identifier is required."))
        endpoint = self.endpoints.history_endpoint.replace("{systemId}", identifier).replace("{system_id}", identifier)
        response = self._request("GET", endpoint, params={"level": level, "date": target_date.isoformat()})
        data = _response_data(response.payload)
        if not isinstance(data, dict):
            raise SigenergyClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy history response is invalid."))
        return data

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> SigenergyHttpResponse:
        headers = {"Accept": "application/json", "sigen-region": self.endpoints.region}
        if json_payload is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            if not self._access_token:
                raise SigenergyClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "Sigenergy authentication is required."))
            headers["Authorization"] = f"Bearer {self._access_token}"
        response = self.transport.request(
            method,
            _url(self.endpoints.base_url, endpoint),
            params=params,
            headers=headers,
            json_payload=json_payload,
            timeout_seconds=self.timeout_seconds,
        )
        if response.status_code in {401, 403}:
            code = ProviderErrorCode.AUTHENTICATION if response.status_code == 401 else ProviderErrorCode.AUTHORIZATION
            raise SigenergyClientError(ProviderError(code, "Sigenergy request was not authorized."))
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response.headers)
            raise SigenergyClientError(ProviderError(ProviderErrorCode.RATE_LIMITED, "Sigenergy request was rate limited.", retry_after_seconds=retry_after, transient=True))
        if response.status_code >= 500:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.UNAVAILABLE, "Sigenergy is temporarily unavailable.", transient=True))
        if response.status_code >= 400:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.TRANSPORT, "Sigenergy request failed."))
        code = response.payload.get("code")
        if code not in (None, 0, "0"):
            if str(code) in {"1201", "1401"}:
                raise SigenergyClientError(ProviderError(ProviderErrorCode.AUTHORIZATION, "Sigenergy access was restricted."))
            raise SigenergyClientError(ProviderError(ProviderErrorCode.UNKNOWN, f"Sigenergy provider returned error code {str(code)[:32]}"))
        return response


def _url(base_url: str, endpoint: str) -> str:
    if not base_url.startswith(("https://", "http://")) or not endpoint.strip():
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy endpoint configuration is invalid."))
    parsed = urlparse(base_url)
    if not parsed.netloc:
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy base URL is invalid."))
    return urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/"))


def _response_data(payload: dict[str, Any]) -> Any:
    data = payload.get("data")
    if isinstance(data, str):
        if not data.strip():
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise SigenergyClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "Sigenergy response data is not valid JSON.")) from exc
    return data


def _rows_from_data(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "systems", "items", "systemList", "rows"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        if any(key in data for key in ("systemId", "id", "systemName", "name")):
            return [data]
    return []


def _retry_after_seconds(headers: dict[str, str]) -> int | None:
    raw = next((value for key, value in headers.items() if key.casefold() == "retry-after"), "").strip()
    if not raw:
        return None
    try:
        return max(int(raw), 0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(int((retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()), 0)
        except (TypeError, ValueError, OverflowError):
            return None
