from __future__ import annotations

import base64
import threading
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Protocol

import requests

from monitoring_board.preview_safety import require_external_actions_enabled
from monitoring_board.services.api_client_base import http_rate_limited_status, http_retryable_status, retry_api_call
from monitoring_board.services.api_rate_limit import ApiRateLimitError, ApiTransientError
from monitoring_board.services.sigenergy_errors import SigenergyApiError, SigenergyAuthError
from monitoring_board.services.sigenergy_models import (
    DEFAULT_TOKEN_TTL_SECONDS,
    SigenergyCredentials,
    SigenergyEndpoints,
    build_sigenergy_url,
    parse_sigenergy_response,
    sanitize_sigenergy_error,
)


_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_TOKEN_LOCK = threading.Lock()


class SigenergyRequestPolicy(Protocol):
    def validate_endpoints(self, endpoints: SigenergyEndpoints) -> None: ...

    def authorize_login(self, endpoint: str) -> None: ...

    def authorize_request(self, method: str, endpoint: str) -> None: ...

    def authorize_system_id(self, system_id: str) -> None: ...

    def filter_discovered_systems(
        self,
        systems: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


def clear_token_cache_for_tests() -> None:
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()


def auth_headers(region: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Accept": "application/json", "sigen-region": region}


def bearer_headers(access_token: str, region: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}", "sigen-region": region}


class SigenergyClient:
    def __init__(
        self,
        endpoints: SigenergyEndpoints | dict[str, Any],
        credentials: SigenergyCredentials | None = None,
        *,
        session: requests.Session | None = None,
        token_cache: dict[str, dict[str, Any]] | None = None,
        token_lock: threading.Lock | None = None,
        allow_sleep: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
        read_only_policy: SigenergyRequestPolicy | None = None,
    ) -> None:
        if isinstance(endpoints, dict):
            config = endpoints
            self.endpoints = SigenergyEndpoints(
                base_url=str(config.get("base_url") or ""),
                login_endpoint=str(config.get("login_endpoint") or ""),
                systems_endpoint=str(config.get("systems_endpoint") or config.get("plants_endpoint") or ""),
                energy_flow_endpoint=str(config.get("energy_flow_endpoint") or ""),
                history_endpoint=str(
                    config.get("history_endpoint")
                    or "/openapi/systems/{system_id}/history"
                ),
                region=str(config.get("region") or "eu"),
            )
            self.credentials = SigenergyCredentials(
                app_key=str(config.get("username") or config.get("app_key") or ""),
                app_secret=str(config.get("password") or config.get("app_secret") or ""),
            )
        else:
            self.endpoints = endpoints
            self.credentials = credentials or SigenergyCredentials("", "")
        self.session = session or requests.Session()
        self.token_cache = token_cache if token_cache is not None else _TOKEN_CACHE
        self.token_lock = token_lock or _TOKEN_LOCK
        self.allow_sleep = allow_sleep
        self.sleeper = sleeper
        self.read_only_policy = read_only_policy
        if self.read_only_policy is not None:
            self.read_only_policy.validate_endpoints(self.endpoints)

    @property
    def cache_key(self) -> str:
        return f"{self.endpoints.base_url}|{self.credentials.app_key}|{self.endpoints.region}"

    def invalidate_access_token(self) -> None:
        with self.token_lock:
            self.token_cache.pop(self.cache_key, None)

    def authenticate(self) -> str:
        return self.get_access_token(force_login=True)

    def get_access_token(
        self,
        *,
        force_login: bool = False,
        api_area: str = "state",
    ) -> str:
        now = datetime.now()
        with self.token_lock:
            cached = self.token_cache.get(self.cache_key)
            if cached and not force_login and cached["expires_at"] > now:
                return str(cached["access_token"])

        data = self._authenticate_payload(api_area=api_area)
        access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        if not access_token:
            raise SigenergyApiError("A resposta Sigenergy de login nao trouxe accessToken.", payload=data)
        raw_ttl = data.get("expiresIn") or data.get("expires_in") or data.get("expires")
        try:
            ttl_seconds = int(float(str(raw_ttl))) if raw_ttl not in (None, "") else DEFAULT_TOKEN_TTL_SECONDS
        except (TypeError, ValueError):
            ttl_seconds = DEFAULT_TOKEN_TTL_SECONDS
        expires_at = now + timedelta(seconds=max(min(ttl_seconds, DEFAULT_TOKEN_TTL_SECONDS) - 60, 300))
        with self.token_lock:
            self.token_cache[self.cache_key] = {"access_token": access_token, "expires_at": expires_at}
        return access_token

    def request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: Any | None = None,
        validate_code: bool = True,
        refreshed: bool = False,
        api_area: str = "state",
    ) -> dict[str, Any]:
        if self.read_only_policy is None:
            require_external_actions_enabled()
        else:
            self.read_only_policy.authorize_request(method, endpoint)
        token = self.get_access_token(api_area=api_area)

        def call_once() -> requests.Response:
            try:
                response = self.session.request(
                    method,
                    build_sigenergy_url(self.endpoints.base_url, endpoint),
                    headers=bearer_headers(token, self.endpoints.region),
                    params=params,
                    json=json_payload,
                    timeout=30,
                )
                response.raise_for_status()
                return response
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code == 401 and not refreshed:
                    raise SigenergyAuthError("Sigenergy HTTP 401.", status_code=status_code) from exc
                if http_rate_limited_status(status_code):
                    raise sigenergy_rate_limit_error(
                        "Sigenergy HTTP 429.",
                        response=exc.response,
                        api_area=api_area,
                    ) from exc
                if http_retryable_status(status_code):
                    raise ApiTransientError(f"Sigenergy HTTP {status_code}") from exc
                raise SigenergyApiError(sanitize_sigenergy_error(exc), status_code=status_code, error_type="http") from exc
            except requests.RequestException as exc:
                raise ApiTransientError(f"Sigenergy request failed: {sanitize_sigenergy_error(exc)}") from exc

        try:
            response = retry_api_call(call_once, allow_sleep=self.allow_sleep, sleeper=self.sleeper)
        except SigenergyAuthError:
            if refreshed:
                raise
            self.invalidate_access_token()
            self.get_access_token(force_login=True, api_area=api_area)
            return self.request_json(
                method,
                endpoint,
                params=params,
                json_payload=json_payload,
                validate_code=validate_code,
                refreshed=True,
                api_area=api_area,
            )
        payload = self._json_response(response)
        if validate_code:
            parse_sigenergy_response(payload)
        return payload

    def list_systems(self, *, allow_empty: bool = False) -> list[dict[str, Any]]:
        payload = self.request_json(
            "GET",
            self.endpoints.systems_endpoint,
            api_area="discovery",
        )
        data = parse_sigenergy_response(payload)
        rows = _strict_system_rows(data)
        if self.read_only_policy is not None:
            rows = self.read_only_policy.filter_discovered_systems(rows)
        if not rows and not allow_empty:
            raise SigenergyApiError(
                "A API Sigenergy respondeu com sucesso, mas sem sistemas. Confirma se a App Key tem sistemas autorizados.",
                payload=payload,
            )
        return rows

    def get_energy_flow(self, system_id: str) -> dict[str, Any]:
        if self.read_only_policy is not None:
            self.read_only_policy.authorize_system_id(system_id)
        endpoint = self.endpoints.energy_flow_endpoint.replace("{systemId}", "{system_id}")
        endpoint = endpoint.replace("{system_id}", str(system_id))
        payload = self.request_json("GET", endpoint, api_area="state")
        data = parse_sigenergy_response(payload)
        if not isinstance(data, dict):
            raise SigenergyApiError(
                "O energyFlow Sigenergy nao devolveu um objeto de dados."
            )
        return data

    def get_system_history(
        self,
        system_id: str,
        *,
        level: str,
        target_date: str | None = None,
    ) -> dict[str, Any]:
        if self.read_only_policy is not None:
            self.read_only_policy.authorize_system_id(system_id)
        endpoint = self.endpoints.history_endpoint.replace(
            "{systemId}",
            "{system_id}",
        ).replace("{system_id}", str(system_id))
        request_payload: dict[str, Any] = {"level": level}
        if target_date:
            request_payload["date"] = target_date
        payload = self.request_json(
            "GET",
            endpoint,
            params=request_payload,
            api_area="production",
        )
        data = parse_sigenergy_response(payload)
        if not isinstance(data, dict):
            raise SigenergyApiError(
                "O historico Sigenergy nao devolveu um objeto de dados."
            )
        return data

    def _authenticate_payload(self, *, api_area: str = "state") -> dict[str, Any]:
        if self.read_only_policy is None:
            require_external_actions_enabled()
        else:
            self.read_only_policy.authorize_login(
                self.endpoints.login_endpoint
            )
        app_key = self.credentials.app_key.strip()
        app_secret = self.credentials.app_secret.strip()
        if not app_key or not app_secret:
            raise SigenergyApiError("Preenche App Key e App Secret da Sigenergy.")
        encoded_key = base64.b64encode(f"{app_key}:{app_secret}".encode("utf-8")).decode("ascii")

        def call_once() -> requests.Response:
            try:
                response = self.session.post(
                    build_sigenergy_url(self.endpoints.base_url, self.endpoints.login_endpoint),
                    json={"key": encoded_key},
                    headers=auth_headers(self.endpoints.region),
                    timeout=30,
                )
                response.raise_for_status()
                return response
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code in {401, 403}:
                    raise SigenergyAuthError(
                        f"Sigenergy auth HTTP {status_code}.",
                        status_code=status_code,
                    ) from exc
                if http_rate_limited_status(status_code):
                    raise sigenergy_rate_limit_error(
                        "Sigenergy HTTP 429 auth.",
                        response=exc.response,
                        api_area=api_area,
                    ) from exc
                if http_retryable_status(status_code):
                    raise ApiTransientError(f"Sigenergy auth HTTP {status_code}") from exc
                raise SigenergyApiError(sanitize_sigenergy_error(exc), status_code=status_code, error_type="http") from exc
            except requests.RequestException as exc:
                raise ApiTransientError(f"Sigenergy auth request failed: {sanitize_sigenergy_error(exc)}") from exc

        response = retry_api_call(call_once, allow_sleep=self.allow_sleep, sleeper=self.sleeper)
        data = parse_sigenergy_response(self._json_response(response))
        if not isinstance(data, dict):
            raise SigenergyApiError("A resposta Sigenergy de login nao trouxe data JSON valido.")
        return data

    def _json_response(self, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SigenergyApiError("Resposta Sigenergy invalida: payload JSON inesperado.", error_type="invalid_json") from exc
        if not isinstance(payload, dict):
            raise SigenergyApiError("Resposta Sigenergy invalida: payload JSON inesperado.", error_type="invalid_json")
        return payload


def sigenergy_rate_limit_error(
    message: str,
    *,
    response: requests.Response | None = None,
    api_area: str = "state",
) -> ApiRateLimitError:
    cooldown_until = datetime.now() + timedelta(minutes=60)
    retry_after = ""
    if response is not None:
        retry_after = str(getattr(response, "headers", {}).get("Retry-After") or "").strip()
    if retry_after:
        try:
            cooldown_until = datetime.now() + timedelta(seconds=max(int(retry_after), 1))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                if parsed > datetime.now():
                    cooldown_until = parsed
            except (TypeError, ValueError, OverflowError):
                pass
    return ApiRateLimitError("Sigenergy", api_area, cooldown_until, message)


def _strict_system_rows(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        if any(not isinstance(row, dict) for row in data):
            raise SigenergyApiError(
                "A descoberta Sigenergy devolveu uma lista invalida."
            )
        rows = list(data)
        _validate_system_ids(rows)
        return rows
    if not isinstance(data, dict):
        if isinstance(data, str):
            raise SigenergyApiError(
                "A descoberta Sigenergy devolveu um payload invalido "
                f"(tipo=str, comprimento={len(data)})."
            )
        raise SigenergyApiError(
            "A descoberta Sigenergy devolveu um payload invalido."
        )
    if not data:
        return []
    for key in ("list", "records", "systems", "items", "systemList", "rows"):
        if key not in data:
            continue
        rows = data[key]
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            raise SigenergyApiError(
                "A descoberta Sigenergy devolveu uma lista invalida."
            )
        result = list(rows)
        _validate_system_ids(result)
        return result
    if any(key in data for key in ("systemId", "id", "systemName", "name")):
        rows = [data]
        _validate_system_ids(rows)
        return rows
    raise SigenergyApiError(
        "A descoberta Sigenergy devolveu um payload invalido."
    )


def _validate_system_ids(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        system_id = (
            row.get("systemId")
            or row.get("id")
            or row.get("stationId")
            or row.get("plantId")
        )
        if not str(system_id or "").strip():
            raise SigenergyApiError(
                "A resposta Sigenergy nao trouxe systemId numa das linhas."
            )


def client_from_config(config: dict[str, Any], session: requests.Session | None = None) -> SigenergyClient:
    return SigenergyClient(config, session=session)


def authenticate(config: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    client = client_from_config(config, session=session)
    token = client.get_access_token(force_login=True)
    return {"accessToken": token}


def get_access_token(config: dict[str, Any], session: requests.Session | None = None, *, force_login: bool = False) -> str:
    return client_from_config(config, session=session).get_access_token(force_login=force_login)


def invalidate_access_token(config: dict[str, Any]) -> None:
    client_from_config(config).invalidate_access_token()


def list_systems(
    config: dict[str, Any],
    session: requests.Session | None = None,
    *,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    return client_from_config(config, session=session).list_systems(
        allow_empty=allow_empty
    )


def get_energy_flow(config: dict[str, Any], system_id: str, session: requests.Session | None = None) -> dict[str, Any]:
    return client_from_config(config, session=session).get_energy_flow(system_id)


def get_system_history(
    config: dict[str, Any],
    system_id: str,
    *,
    level: str,
    target_date: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    return client_from_config(config, session=session).get_system_history(
        system_id,
        level=level,
        target_date=target_date,
    )
