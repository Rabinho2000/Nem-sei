from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable

import requests

from monitoring_board.services.api_client_base import http_rate_limited_status, http_retryable_status, retry_api_call
from monitoring_board.services.api_rate_limit import ApiRateLimitError, ApiTransientError
from monitoring_board.services.fusionsolar import build_provider_url
from monitoring_board.services.fusionsolar_errors import (
    FusionSolarApiError,
    FusionSolarCredentialsError,
    FusionSolarRateLimitError,
    FusionSolarSessionExpiredError,
)
from monitoring_board.services.fusionsolar_models import (
    FusionSolarCredentials,
    FusionSolarEndpoints,
    closed_day_window_ms,
    collect_time_noon_of_month_ms,
    collect_time_start_of_day_ms,
    normalize_kpi_rows,
)


LOGGER = logging.getLogger(__name__)


def fail_code(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("failCode"))
    except (TypeError, ValueError):
        return None


def is_rate_limit_payload(payload: dict[str, Any] | None) -> bool:
    message = str((payload or {}).get("message") or "").lower()
    return fail_code(payload) == 407 or "rate limit" in message or "call limit" in message or "too many" in message


def is_session_expired_payload(payload: dict[str, Any] | None) -> bool:
    message = str((payload or {}).get("message") or "")
    return fail_code(payload) == 305 or "USER_MUST_RELOGIN" in message


def is_invalid_credentials_payload(payload: dict[str, Any] | None) -> bool:
    code = fail_code(payload)
    message = str((payload or {}).get("message") or "").lower()
    return code in {201, 302, 303, 304} or "password" in message or "credential" in message or "user name" in message


def extract_xsrf_token(response: requests.Response, session: requests.Session) -> str:
    for key, value in response.headers.items():
        if key.lower() == "xsrf-token" and value:
            return value.strip()
    for cookie_name in ("XSRF-TOKEN", "xsrf-token"):
        cookie_value = session.cookies.get(cookie_name)
        if cookie_value:
            return str(cookie_value).strip()
    raise FusionSolarApiError("O login FusionSolar respondeu sem XSRF-TOKEN no header/cookies.")


class FusionSolarClient:
    def __init__(
        self,
        endpoints: FusionSolarEndpoints,
        credentials: FusionSolarCredentials,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        session_cache: dict[str, Any] | None = None,
        session_lock: threading.Lock | None = None,
        session_cache_minutes: int = 55,
        allow_sleep: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.endpoints = endpoints
        self.credentials = credentials
        self.session_factory = session_factory
        self.session_cache = session_cache if session_cache is not None else {}
        self.session_lock = session_lock or threading.Lock()
        self.session_cache_minutes = max(1, int(session_cache_minutes or 55))
        self.allow_sleep = allow_sleep
        self.sleeper = sleeper

    @property
    def cache_key(self) -> str:
        return f"{self.endpoints.base_url}|{self.credentials.username}"

    def login(self, *, force_login: bool = False) -> tuple[requests.Session, str]:
        if not self.credentials.username or not self.credentials.password:
            raise ValueError("Preenche username e password do FusionSolar.")
        if not self.endpoints.base_url:
            raise ValueError("Preenche a Base URL do FusionSolar.")

        now = datetime.now()
        with self.session_lock:
            cached = self.session_cache.get(self.cache_key)
            if cached and not force_login and cached["expires_at"] > now:
                LOGGER.info("Reusing cached FusionSolar session")
                return cached["session"], cached["xsrf_token"]

            LOGGER.info("FusionSolar login required; cached session missing, expired, or forced")
            session = self.session_factory()
            response = retry_api_call(
                lambda: self._login_once(session),
                allow_sleep=self.allow_sleep,
                sleeper=self.sleeper,
            )
            payload = self._json_response(response, "O login FusionSolar devolveu uma resposta JSON invalida.")
            self._validate_payload(payload, expected_message="Login FusionSolar falhou.", require_data=False)
            xsrf_token = extract_xsrf_token(response, session)
            session.headers.update(
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, */*",
                    "XSRF-TOKEN": xsrf_token,
                }
            )
            self.session_cache[self.cache_key] = {
                "session": session,
                "xsrf_token": xsrf_token,
                "expires_at": now + timedelta(minutes=self.session_cache_minutes),
            }
            return session, xsrf_token

    def invalidate_session(self) -> None:
        with self.session_lock:
            self.session_cache.pop(self.cache_key, None)

    def post_json(
        self,
        session: requests.Session,
        endpoint_or_url: str,
        payload: dict[str, Any],
        *,
        expected_message: str,
        require_data: bool = False,
    ) -> dict[str, Any]:
        url = endpoint_or_url if endpoint_or_url.lower().startswith("http") else build_provider_url(self.endpoints.base_url, endpoint_or_url)

        def call_once() -> dict[str, Any]:
            try:
                response = session.post(url, json=payload, timeout=30)
                response.raise_for_status()
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if http_rate_limited_status(status_code):
                    raise FusionSolarRateLimitError("FusionSolar HTTP 429.", status_code=status_code) from exc
                if http_retryable_status(status_code):
                    raise ApiTransientError(f"FusionSolar HTTP {status_code}") from exc
                raise FusionSolarApiError(
                    f"Erro HTTP na chamada FusionSolar ({status_code or 'sem codigo'}).",
                    status_code=status_code,
                    error_type="http",
                ) from exc
            except requests.RequestException as exc:
                raise ApiTransientError(f"FusionSolar request failed: {exc}") from exc
            data = self._json_response(response, "A API FusionSolar devolveu uma resposta JSON invalida.")
            self._validate_payload(data, expected_message=expected_message, require_data=require_data)
            return data

        return retry_api_call(call_once, allow_sleep=self.allow_sleep, sleeper=self.sleeper)

    def request_with_relogin(
        self,
        operation: Callable[[requests.Session], Any],
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            session, _ = self.login(force_login=attempt == 1)
            try:
                return operation(session)
            except FusionSolarSessionExpiredError as exc:
                last_error = exc
                if attempt == 1:
                    raise
                LOGGER.info("FusionSolar session expired; invalidating cache and retrying login once")
                self.invalidate_session()
        raise last_error or FusionSolarApiError("Falha desconhecida no FusionSolar.")

    def stations(self) -> list[dict[str, Any]]:
        def op(session: requests.Session) -> list[dict[str, Any]]:
            stations: list[dict[str, Any]] = []
            page_no = 1
            page_count = 1
            while page_no <= page_count:
                payload = self.post_json(
                    session,
                    self.endpoints.plants_endpoint,
                    {"pageNo": page_no},
                    expected_message="Falha ao obter a lista de centrais FusionSolar.",
                    require_data=True,
                )
                page_data = payload.get("data") or {}
                page_list = page_data.get("list") or []
                if not isinstance(page_list, list):
                    raise ValueError("A resposta FusionSolar da lista de centrais nao trouxe data.list.")
                stations.extend([item for item in page_list if isinstance(item, dict)])
                page_count = int(page_data.get("pageCount") or 1)
                page_no += 1
            return stations

        return self.request_with_relogin(op)

    def station_realtime_kpi(self, station_codes: list[str]) -> dict[str, dict[str, Any]]:
        def op(session: requests.Session) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            for group in chunked(station_codes, 100):
                payload = self.post_json(
                    session,
                    self.endpoints.real_time_endpoint,
                    {"stationCodes": ",".join(group)},
                    expected_message="Falha ao obter os dados realtime das centrais FusionSolar.",
                    require_data=True,
                )
                rows = payload.get("data") or []
                if not isinstance(rows, list):
                    raise ValueError("A resposta FusionSolar realtime nao trouxe uma lista em data.")
                for row in rows:
                    if isinstance(row, dict) and str(row.get("stationCode") or "").strip():
                        result[str(row["stationCode"]).strip()] = row
            return result

        return self.request_with_relogin(op)

    def device_list(self, station_codes: list[str]) -> list[dict[str, Any]]:
        def op(session: requests.Session) -> list[dict[str, Any]]:
            devices: list[dict[str, Any]] = []
            for group in chunked(station_codes, 100):
                payload = self.post_json(
                    session,
                    self.endpoints.device_list_endpoint,
                    {"stationCodes": ",".join(group)},
                    expected_message="Falha ao obter a lista de dispositivos FusionSolar.",
                    require_data=True,
                )
                data = payload.get("data") or []
                rows = data.get("list") if isinstance(data, dict) else data
                if not isinstance(rows, list):
                    raise ValueError("A resposta FusionSolar de dispositivos nao trouxe uma lista em data.")
                devices.extend([row for row in rows if isinstance(row, dict)])
            return devices

        return self.request_with_relogin(op)

    def device_realtime_kpi(self, devices: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        def op(session: requests.Session) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            devices_by_type: dict[int, list[str]] = {}
            for device in devices:
                dev_type_id = device.get("dev_type_id")
                external_device_id = str(device.get("external_device_id") or "").strip()
                if dev_type_id is None or not external_device_id:
                    continue
                devices_by_type.setdefault(int(dev_type_id), []).append(external_device_id)
            for dev_type_id, device_ids in devices_by_type.items():
                for group in chunked(device_ids, 100):
                    payload = self.post_json(
                        session,
                        self.endpoints.device_real_time_endpoint,
                        {"devIds": ",".join(group), "devTypeId": dev_type_id},
                        expected_message="Falha ao obter os dados realtime dos dispositivos FusionSolar.",
                        require_data=True,
                    )
                    rows = payload.get("data") or []
                    if not isinstance(rows, list):
                        raise ValueError("A resposta FusionSolar realtime de dispositivos nao trouxe uma lista em data.")
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        for key in ("devId", "id", "devDn", "deviceDn", "esnCode", "sn"):
                            value = str(row.get(key) or "").strip()
                            if value:
                                result[value] = row
            return result

        return self.request_with_relogin(op)

    def device_history_kpi(
        self,
        devices: list[dict[str, Any]],
        target_date: date,
        *,
        call_delay_seconds: float = 0,
        sleeper: Callable[[float], None] = time.sleep,
        normalizer: Callable[[Any, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        def op(session: requests.Session) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            calls_made = 0
            start_time, end_time = closed_day_window_ms(target_date)
            devices_by_type: dict[int, list[dict[str, Any]]] = {}
            for device in devices:
                if device.get("dev_type_id") is None or not device.get("external_device_id"):
                    continue
                devices_by_type.setdefault(int(device["dev_type_id"]), []).append(device)
            for dev_type_id, typed_devices in devices_by_type.items():
                for group in [typed_devices[index : index + 10] for index in range(0, len(typed_devices), 10)]:
                    if calls_made and call_delay_seconds > 0:
                        sleeper(call_delay_seconds)
                    payload = self.post_json(
                        session,
                        self.endpoints.device_history_endpoint,
                        {
                            "devIds": ",".join(str(device["external_device_id"]) for device in group),
                            "devTypeId": dev_type_id,
                            "startTime": start_time,
                            "endTime": end_time,
                        },
                        expected_message="Falha ao obter o historico dos inversores FusionSolar.",
                        require_data=True,
                    )
                    calls_made += 1
                    rows.extend((normalizer or _default_history_normalizer)(payload.get("data"), group))
            return rows

        return self.request_with_relogin(op)

    def alarms(self, station_codes: list[str], *, language: str = "en_US") -> dict[str, list[dict[str, Any]]]:
        def op(session: requests.Session) -> dict[str, list[dict[str, Any]]]:
            result: dict[str, list[dict[str, Any]]] = {}
            now_ms = int(datetime.now().timestamp() * 1000)
            for group in chunked(station_codes, 100):
                payload = self.post_json(
                    session,
                    self.endpoints.alarms_endpoint,
                    {
                        "stationCodes": ",".join(group),
                        "beginTime": 0,
                        "endTime": now_ms,
                        "language": language,
                    },
                    expected_message="Falha ao obter alarmes ativos FusionSolar.",
                    require_data=True,
                )
                rows = payload.get("data") or []
                if not isinstance(rows, list):
                    raise ValueError("A resposta FusionSolar de alarmes nao trouxe uma lista em data.")
                for row in rows:
                    if isinstance(row, dict) and str(row.get("stationCode") or "").strip():
                        result.setdefault(str(row["stationCode"]).strip(), []).append(row)
            return result

        return self.request_with_relogin(op)

    def station_day_kpi_map(self, station_codes: list[str], collect_date: date) -> dict[str, dict[str, Any]]:
        return self._station_kpi_map(
            self.endpoints.day_kpi_endpoint,
            station_codes,
            collect_date,
            "Falha ao obter os KPIs diarios FusionSolar.",
        )

    def station_month_kpi_map(self, station_codes: list[str], collect_date: date) -> dict[str, dict[str, Any]]:
        return self._station_kpi_map(
            self.endpoints.month_kpi_endpoint,
            station_codes,
            collect_date.replace(day=1),
            "Falha ao obter os KPIs mensais FusionSolar.",
        )

    def station_day_kpi_rows(self, station_codes: list[str], collect_date: date) -> list[dict[str, Any]]:
        def op(session: requests.Session) -> list[dict[str, Any]]:
            payload = self.post_json(
                session,
                self.endpoints.day_kpi_endpoint,
                {
                    "stationCodes": ",".join(station_codes),
                    "collectTime": collect_time_noon_of_month_ms(collect_date),
                },
                expected_message="Falha ao obter os KPIs diarios FusionSolar.",
                require_data=True,
            )
            return _enrich_kpi_rows(normalize_kpi_rows(payload.get("data")))

        return self.request_with_relogin(op)

    def _station_kpi_map(
        self,
        endpoint: str,
        station_codes: list[str],
        collect_date: date,
        expected_message: str,
    ) -> dict[str, dict[str, Any]]:
        def op(session: requests.Session) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            for group in chunked(station_codes, 100):
                payload = self.post_json(
                    session,
                    endpoint,
                    {
                        "stationCodes": ",".join(group),
                        "collectTime": collect_time_start_of_day_ms(collect_date),
                    },
                    expected_message=expected_message,
                    require_data=True,
                )
                for row in _enrich_kpi_rows(normalize_kpi_rows(payload.get("data"))):
                    station_code = str(row.get("stationCode") or row.get("plantCode") or "").strip()
                    if station_code:
                        result[station_code] = row
            return result

        return self.request_with_relogin(op)

    def _login_once(self, session: requests.Session) -> requests.Response:
        try:
            response = session.post(
                build_provider_url(self.endpoints.base_url, self.endpoints.login_endpoint),
                json={"userName": self.credentials.username, "systemCode": self.credentials.password},
                headers={"Content-Type": "application/json", "Accept": "application/json, */*"},
                timeout=30,
            )
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            status_code = getattr(exc.response, "status_code", None)
            if http_rate_limited_status(status_code):
                raise FusionSolarRateLimitError("FusionSolar HTTP 429 login.", status_code=status_code) from exc
            if http_retryable_status(status_code):
                raise ApiTransientError(f"FusionSolar login HTTP {status_code}") from exc
            raise FusionSolarApiError(
                f"Erro HTTP no login FusionSolar ({status_code or 'sem codigo'}).",
                status_code=status_code,
                error_type="http",
            ) from exc
        except requests.RequestException as exc:
            raise ApiTransientError(f"FusionSolar login request failed: {exc}") from exc

    def _json_response(self, response: requests.Response, invalid_message: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FusionSolarApiError(invalid_message, error_type="invalid_json") from exc
        if not isinstance(payload, dict):
            raise FusionSolarApiError(invalid_message, error_type="invalid_json")
        return payload

    def _validate_payload(self, payload: dict[str, Any], *, expected_message: str, require_data: bool) -> None:
        if payload.get("success") is not True or int(payload.get("failCode") or 0) != 0:
            message = payload.get("message") or expected_message
            if is_rate_limit_payload(payload):
                raise FusionSolarRateLimitError(f"{message} (failCode={payload.get('failCode')})", payload=payload)
            if is_session_expired_payload(payload):
                raise FusionSolarSessionExpiredError(f"{message} (failCode={payload.get('failCode')})", payload=payload)
            if is_invalid_credentials_payload(payload):
                raise FusionSolarCredentialsError("Credenciais FusionSolar invalidas.", payload=payload)
            raise FusionSolarApiError(f"{message} (failCode={payload.get('failCode')})", payload=payload)
        if require_data and "data" not in payload:
            raise FusionSolarApiError(f"{expected_message} A resposta nao trouxe data.", payload=payload)


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _enrich_kpi_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        enriched["payload_json"] = json.dumps(row, ensure_ascii=True)
        result.append(enriched)
    return result


def _default_history_normalizer(data: Any, _devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def client_from_config(
    config: dict[str, Any],
    endpoints: FusionSolarEndpoints,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
    session_cache: dict[str, Any] | None = None,
    session_lock: threading.Lock | None = None,
    session_cache_minutes: int = 55,
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> FusionSolarClient:
    return FusionSolarClient(
        endpoints,
        FusionSolarCredentials(
            username=str(config.get("username") or "").strip(),
            password=str(config.get("password") or "").strip(),
        ),
        session_factory=session_factory,
        session_cache=session_cache,
        session_lock=session_lock,
        session_cache_minutes=session_cache_minutes,
        allow_sleep=allow_sleep,
        sleeper=sleeper,
    )


def endpoint_client(
    base_url: str,
    endpoint: str = "",
    *,
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> FusionSolarClient:
    endpoints = FusionSolarEndpoints(
        base_url=base_url,
        login_endpoint=endpoint,
        plants_endpoint=endpoint,
        real_time_endpoint=endpoint,
        device_list_endpoint=endpoint,
        device_real_time_endpoint=endpoint,
        device_history_endpoint=endpoint,
        alarms_endpoint=endpoint,
        day_kpi_endpoint=endpoint,
        month_kpi_endpoint=endpoint,
    )
    return FusionSolarClient(endpoints, FusionSolarCredentials("", ""), allow_sleep=allow_sleep, sleeper=sleeper)


def post_fusionsolar_json(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    expected_message: str,
    require_data: bool = False,
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    return endpoint_client("", "", allow_sleep=allow_sleep, sleeper=sleeper).post_json(
        session,
        url,
        payload,
        expected_message=expected_message,
        require_data=require_data,
    )


def fetch_stations(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    stations: list[dict[str, Any]] = []
    page_no = 1
    page_count = 1
    while page_no <= page_count:
        payload = client.post_json(
            session,
            endpoint,
            {"pageNo": page_no},
            expected_message="Falha ao obter a lista de centrais FusionSolar.",
            require_data=True,
        )
        page_data = payload.get("data") or {}
        page_list = page_data.get("list") or []
        if not isinstance(page_list, list):
            raise ValueError("A resposta FusionSolar da lista de centrais nao trouxe data.list.")
        stations.extend([item for item in page_list if isinstance(item, dict)])
        page_count = int(page_data.get("pageCount") or 1)
        page_no += 1
    return stations


def fetch_realtime_map(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    station_codes: list[str],
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, Any]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    result: dict[str, dict[str, Any]] = {}
    for group in chunked(station_codes, 100):
        payload = client.post_json(
            session,
            endpoint,
            {"stationCodes": ",".join(group)},
            expected_message="Falha ao obter os dados realtime das centrais FusionSolar.",
            require_data=True,
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise ValueError("A resposta FusionSolar realtime nao trouxe uma lista em data.")
        for row in rows:
            if isinstance(row, dict) and str(row.get("stationCode") or "").strip():
                result[str(row["stationCode"]).strip()] = row
    return result


def fetch_device_list(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    station_codes: list[str],
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    devices: list[dict[str, Any]] = []
    for group in chunked(station_codes, 100):
        payload = client.post_json(
            session,
            endpoint,
            {"stationCodes": ",".join(group)},
            expected_message="Falha ao obter a lista de dispositivos FusionSolar.",
            require_data=True,
        )
        data = payload.get("data") or []
        rows = data.get("list") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("A resposta FusionSolar de dispositivos nao trouxe uma lista em data.")
        devices.extend([row for row in rows if isinstance(row, dict)])
    return devices


def fetch_device_realtime_map(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    devices: list[dict[str, Any]],
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, Any]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    result: dict[str, dict[str, Any]] = {}
    devices_by_type: dict[int, list[str]] = {}
    for device in devices:
        dev_type_id = device.get("dev_type_id")
        external_device_id = str(device.get("external_device_id") or "").strip()
        if dev_type_id is None or not external_device_id:
            continue
        devices_by_type.setdefault(int(dev_type_id), []).append(external_device_id)
    for dev_type_id, device_ids in devices_by_type.items():
        for group in chunked(device_ids, 100):
            payload = client.post_json(
                session,
                endpoint,
                {"devIds": ",".join(group), "devTypeId": dev_type_id},
                expected_message="Falha ao obter os dados realtime dos dispositivos FusionSolar.",
                require_data=True,
            )
            rows = payload.get("data") or []
            if not isinstance(rows, list):
                raise ValueError("A resposta FusionSolar realtime de dispositivos nao trouxe uma lista em data.")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for key in ("devId", "id", "devDn", "deviceDn", "esnCode", "sn"):
                    value = str(row.get(key) or "").strip()
                    if value:
                        result[value] = row
    return result


def fetch_device_history(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    devices: list[dict[str, Any]],
    target_date: date,
    call_delay_seconds: float = 0,
    sleeper: Callable[[float], None] = time.sleep,
    allow_sleep: bool = False,
    normalizer: Callable[[Any, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    start_time, end_time = closed_day_window_ms(target_date)
    rows: list[dict[str, Any]] = []
    calls_made = 0
    devices_by_type: dict[int, list[dict[str, Any]]] = {}
    for device in devices:
        if device.get("dev_type_id") is None or not device.get("external_device_id"):
            continue
        devices_by_type.setdefault(int(device["dev_type_id"]), []).append(device)
    for dev_type_id, typed_devices in devices_by_type.items():
        for group in [typed_devices[index : index + 10] for index in range(0, len(typed_devices), 10)]:
            if calls_made and call_delay_seconds > 0:
                sleeper(call_delay_seconds)
            payload = client.post_json(
                session,
                endpoint,
                {
                    "devIds": ",".join(str(device["external_device_id"]) for device in group),
                    "devTypeId": dev_type_id,
                    "startTime": start_time,
                    "endTime": end_time,
                },
                expected_message="Falha ao obter o historico dos inversores FusionSolar.",
                require_data=True,
            )
            calls_made += 1
            rows.extend((normalizer or _default_history_normalizer)(payload.get("data"), group))
    return rows


def fetch_alarm_map(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    station_codes: list[str],
    language: str = "en_US",
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, list[dict[str, Any]]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    result: dict[str, list[dict[str, Any]]] = {}
    now_ms = int(datetime.now().timestamp() * 1000)
    for group in chunked(station_codes, 100):
        payload = client.post_json(
            session,
            endpoint,
            {"stationCodes": ",".join(group), "beginTime": 0, "endTime": now_ms, "language": language},
            expected_message="Falha ao obter alarmes ativos FusionSolar.",
            require_data=True,
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise ValueError("A resposta FusionSolar de alarmes nao trouxe uma lista em data.")
        for row in rows:
            if isinstance(row, dict) and str(row.get("stationCode") or "").strip():
                result.setdefault(str(row["stationCode"]).strip(), []).append(row)
    return result


def fetch_kpi_map(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    station_codes: list[str],
    collect_date: date,
    expected_message: str,
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, Any]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    result: dict[str, dict[str, Any]] = {}
    for group in chunked(station_codes, 100):
        payload = client.post_json(
            session,
            endpoint,
            {"stationCodes": ",".join(group), "collectTime": collect_time_start_of_day_ms(collect_date)},
            expected_message=expected_message,
            require_data=True,
        )
        for row in _enrich_kpi_rows(normalize_kpi_rows(payload.get("data"))):
            station_code = str(row.get("stationCode") or row.get("plantCode") or "").strip()
            if station_code:
                result[station_code] = row
    return result


def fetch_kpi_rows(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    station_codes: list[str],
    collect_date: date,
    expected_message: str,
    allow_sleep: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    client = endpoint_client(base_url, endpoint, allow_sleep=allow_sleep, sleeper=sleeper)
    payload = client.post_json(
        session,
        endpoint,
        {"stationCodes": ",".join(station_codes), "collectTime": collect_time_noon_of_month_ms(collect_date)},
        expected_message=expected_message,
        require_data=True,
    )
    return _enrich_kpi_rows(normalize_kpi_rows(payload.get("data")))
