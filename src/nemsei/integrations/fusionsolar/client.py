"""Small FusionSolar HTTP boundary for implemented read capabilities only.

The endpoint shapes below are limited to behavior observed in frozen V1 fixtures.
Plant discovery, current plant monitoring, one daily production endpoint, and
(M7 Fatia 2) device discovery and device-level current KPIs are implemented --
all evidenced by V1's own working code (`monitoring_board/services/
fusionsolar_client.py`: `fetch_device_list`, `fetch_device_realtime_map`), not
by documentation this codebase has never seen. The caller owns
production-period and unit contracts; this HTTP boundary never guesses them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from http.cookiejar import CookieJar
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

from nemsei.providers.errors import ProviderError, ProviderErrorCode


@dataclass(frozen=True)
class FusionSolarCredentials:
    username: str
    password: str
    base_url: str


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    payload: dict[str, Any]


class FusionSolarTransport(Protocol):
    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> HttpResponse: ...


class FusionSolarClientError(Exception):
    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.safe_message)
        self.error = error


class UrllibFusionSolarTransport:
    """Stateful cookie-aware production transport; tests inject a deterministic fake."""

    def __init__(self) -> None:
        self._cookies = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookies))

    def xsrf_token(self) -> str | None:
        for cookie in self._cookies:
            if cookie.name.casefold() == "xsrf-token" and cookie.value.strip():
                return cookie.value.strip()
        return None

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> HttpResponse:
        request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return self._response(response.status, dict(response.headers.items()), response.read())
        except HTTPError as exc:
            body = exc.read()
            response = self._response(exc.code, dict(exc.headers.items()) if exc.headers else {}, body, allow_invalid=True)
            raise FusionSolarClientError(_http_error(response)) from exc
        except TimeoutError as exc:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.TIMEOUT, "FusionSolar request timed out.", transient=True)) from exc
        except URLError as exc:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.UNAVAILABLE, "FusionSolar is unreachable.", transient=True)) from exc

    @staticmethod
    def _response(status: int, headers: dict[str, str], body: bytes, *, allow_invalid: bool = False) -> HttpResponse:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if allow_invalid:
                value = {}
            else:
                raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar returned invalid JSON.")) from exc
        if not isinstance(value, dict):
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar returned an unexpected response."))
        return HttpResponse(status, {str(key): str(value) for key, value in headers.items()}, value)


@dataclass
class FusionSolarClient:
    credentials: FusionSolarCredentials
    transport: FusionSolarTransport = field(default_factory=UrllibFusionSolarTransport)
    timeout_seconds: int = 30
    _token: str | None = field(default=None, init=False)

    login_endpoint: str = "/thirdData/login"
    plants_endpoint: str = "/thirdData/stations"
    current_monitoring_endpoint: str = "/thirdData/getStationRealKpi"
    daily_production_endpoint: str = "/thirdData/getKpiStationDay"
    device_list_endpoint: str = "/thirdData/getDevList"
    device_current_monitoring_endpoint: str = "/thirdData/getDevRealKpi"

    def authenticate(self) -> None:
        response = self._post(
            self.login_endpoint,
            {"userName": self.credentials.username, "systemCode": self.credentials.password},
            include_token=False,
        )
        self._validate(response, phase="authentication")
        self._token = _header(response.headers, "XSRF-TOKEN") or _transport_token(self.transport)

    def discover_page(self, page_number: int, page_size: int = 100) -> tuple[list[dict[str, Any]], int]:
        if self._token is None:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar authentication is required."))
        response = self._post(self.plants_endpoint, {"pageNo": page_number, "pageSize": page_size}, include_token=True)
        self._validate(response, phase="discovery")
        data = response.payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar discovery response has no plant list."))
        page_count = data.get("pageCount", 1)
        try:
            pages = int(page_count)
        except (TypeError, ValueError) as exc:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar discovery page count is invalid.")) from exc
        if pages < page_number or pages < 1:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar discovery page range is invalid."))
        return [row for row in data["list"] if isinstance(row, dict)], pages

    def current_monitoring_batch(self, station_codes: list[str]) -> list[dict[str, Any]]:
        """Read one verified account-level plant-state batch (at most 100 IDs)."""
        if self._token is None:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar authentication is required."))
        codes = [code.strip() for code in station_codes if code and code.strip()]
        if not codes or len(codes) > 100:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar monitoring batch must contain one to 100 station codes."))
        response = self._post(
            self.current_monitoring_endpoint,
            {"stationCodes": ",".join(codes)},
            include_token=True,
        )
        self._validate(response, phase="current_monitoring")
        rows = response.payload.get("data")
        if not isinstance(rows, list):
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar monitoring response has no plant list."))
        return [row for row in rows if isinstance(row, dict)]

    def device_list_batch(self, station_codes: list[str]) -> list[dict[str, Any]]:
        """Read one verified device inventory batch (at most 100 station codes).

        Mirrors V1's `fetch_device_list`: one `stationCodes` payload returns
        every device V1's account can see across those plants, inverters and
        otherwise alike -- the `devTypeId` filter to inverters only happens on
        the caller side, exactly as V1 filtered on `FUSIONSOLAR_INVERTER_
        DEVICE_TYPE_IDS` after this call, not before it.
        """
        if self._token is None:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar authentication is required."))
        codes = [code.strip() for code in station_codes if code and code.strip()]
        if not codes or len(codes) > 100:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar device list batch must contain one to 100 station codes."))
        response = self._post(self.device_list_endpoint, {"stationCodes": ",".join(codes)}, include_token=True)
        self._validate(response, phase="device_discovery")
        data = response.payload.get("data")
        rows = data.get("list") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar device list response has no device list."))
        return [row for row in rows if isinstance(row, dict)]

    def device_current_monitoring_batch(self, device_ids: list[str], *, device_type_id: int) -> list[dict[str, Any]]:
        """Read one verified device-level current-KPI batch for one device type.

        `getDevRealKpi` groups by `devTypeId` (V1: `fetch_device_realtime_map`),
        so a caller must partition mixed device types into one call per type
        rather than sending them together; this method does not partition for
        the caller, to keep its contract as narrow and literal as the plant
        equivalent above.
        """
        if self._token is None:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar authentication is required."))
        ids = [value.strip() for value in device_ids if value and value.strip()]
        if not ids or len(ids) > 100:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar device monitoring batch must contain one to 100 device IDs."))
        response = self._post(
            self.device_current_monitoring_endpoint,
            {"devIds": ",".join(ids), "devTypeId": device_type_id},
            include_token=True,
        )
        self._validate(response, phase="device_current_monitoring")
        rows = response.payload.get("data")
        if not isinstance(rows, list):
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar device monitoring response has no device list."))
        return [row for row in rows if isinstance(row, dict)]

    def daily_production_batch(
        self,
        station_codes: list[str],
        *,
        source_day: date,
        source_timezone: ZoneInfo,
    ) -> list[dict[str, Any]]:
        """Read one daily-KPI batch for one explicitly contracted source day.

        Frozen V1 fixtures establish the endpoint, ``stationCodes`` payload,
        and a maximum batch size of 100.  They do *not* establish which time
        zone defines a provider day, so callers must supply an explicit IANA
        time zone verified for this connection before this method is used.
        """
        if self._token is None:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar authentication is required."))
        codes = [code.strip() for code in station_codes if code and code.strip()]
        if not codes or len(codes) > 100:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar production batch must contain one to 100 station codes."))
        collect_time = datetime.combine(source_day, time.min, tzinfo=source_timezone)
        response = self._post(
            self.daily_production_endpoint,
            {
                "stationCodes": ",".join(codes),
                "collectTime": int(collect_time.timestamp() * 1000),
            },
            include_token=True,
        )
        self._validate(response, phase="production_history")
        rows = response.payload.get("data")
        if not isinstance(rows, list):
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar daily production response has no plant list."))
        return [row for row in rows if isinstance(row, dict)]

    def _post(self, endpoint: str, payload: dict[str, Any], *, include_token: bool) -> HttpResponse:
        headers = {"Content-Type": "application/json", "Accept": "application/json, */*"}
        if include_token and self._token:
            headers["XSRF-TOKEN"] = self._token
        return self.transport.post(_url(self.credentials.base_url, endpoint), payload, headers, self.timeout_seconds)

    @staticmethod
    def _validate(response: HttpResponse, *, phase: str) -> None:
        if response.status_code >= 400:
            raise FusionSolarClientError(_http_error(response))
        payload = response.payload
        if payload.get("success") is True and _int(payload.get("failCode"), 0) == 0:
            return
        fail_code = _int(payload.get("failCode"), None)
        message = str(payload.get("message") or "FusionSolar rejected the request.")
        if fail_code == 407 or "rate" in message.casefold():
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.RATE_LIMITED, "FusionSolar rate limited the request.", retry_after_seconds=_retry_after(response.headers), transient=True))
        if phase == "authentication" and fail_code in {201, 302, 303, 304, 305}:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar credentials were rejected."))
        # A reused session can die between logins during ordinary use --
        # failCode 305 / "USER_MUST_RELOGIN" means exactly that regardless of
        # which endpoint returned it, not only the login endpoint itself
        # (V1's own `is_session_expired_payload` in monitoring_board/services/
        # api_rate_limit.py checks this the same way, phase-independent).
        # Session reuse (session_cache.py) relies on this classification to
        # know when to invalidate a cached session and force a fresh login,
        # rather than silently retrying with a session the provider already
        # discarded.
        if fail_code == 305 or "USER_MUST_RELOGIN" in message:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar session expired; re-authentication is required."))
        if response.status_code == 403 or fail_code in {401, 403}:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.AUTHORIZATION, "FusionSolar access was denied."))
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar rejected an unrecognized request."))


def _url(base_url: str, endpoint: str) -> str:
    if not base_url.startswith(("https://", "http://")):
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "FusionSolar base URL is invalid."))
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def _header(headers: dict[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted and value.strip():
            return value.strip()
    return None


def _transport_token(transport: FusionSolarTransport) -> str | None:
    getter = getattr(transport, "xsrf_token", None)
    return getter() if callable(getter) else None


def _int(value: Any, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _retry_after(headers: dict[str, str]) -> int | None:
    value = _header(headers, "Retry-After")
    try:
        return max(0, int(value)) if value is not None else None
    except ValueError:
        return None


def _http_error(response: HttpResponse) -> ProviderError:
    if response.status_code == 429:
        return ProviderError(ProviderErrorCode.RATE_LIMITED, "FusionSolar rate limited the request.", retry_after_seconds=_retry_after(response.headers), transient=True)
    if response.status_code in {401, 407}:
        return ProviderError(ProviderErrorCode.AUTHENTICATION, "FusionSolar credentials were rejected.")
    if response.status_code == 403:
        return ProviderError(ProviderErrorCode.AUTHORIZATION, "FusionSolar access was denied.")
    if response.status_code >= 500:
        return ProviderError(ProviderErrorCode.UNAVAILABLE, "FusionSolar is temporarily unavailable.", transient=True)
    return ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar returned an unexpected HTTP response.")
