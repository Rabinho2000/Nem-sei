from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime, timedelta
from typing import Any

import requests


DEFAULT_TOKEN_TTL_SECONDS = 20 * 60


class SigenergyAPIError(Exception):
    pass


_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_TOKEN_LOCK = threading.Lock()
_SENSITIVE_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r'("Authorization"\s*:\s*")[^"]+', re.IGNORECASE),
    re.compile(r"('Authorization'\s*:\s*')[^']+", re.IGNORECASE),
    re.compile(r'("key"\s*:\s*")[^"]+', re.IGNORECASE),
    re.compile(r"('key'\s*:\s*')[^']+", re.IGNORECASE),
)


def sanitize_sigenergy_error(value: Any) -> str:
    message = str(value or "")
    for pattern in _SENSITIVE_PATTERNS:
        message = pattern.sub(r"\1[redacted]", message)
    return message.replace("\n", " ").replace("\r", " ").strip()


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in {"authorization", "accesstoken", "access_token", "token", "appsecret", "password", "key"}:
                result[key] = "[redacted]"
            else:
                result[key] = sanitize_payload(item)
        return result
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_sigenergy_error(value)
    return value


def clear_token_cache_for_tests() -> None:
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()


def build_sigenergy_url(base_url: str, endpoint: str, **path_values: str) -> str:
    if not base_url or not endpoint:
        raise SigenergyAPIError("Configura a Base URL e os endpoints da API Sigenergy antes de sincronizar.")
    resolved_endpoint = endpoint
    for key, value in path_values.items():
        resolved_endpoint = resolved_endpoint.replace("{" + key + "}", str(value))
    return f"{base_url.rstrip('/')}/{resolved_endpoint.lstrip('/')}"


def parse_sigenergy_response(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise SigenergyAPIError("Resposta Sigenergy invalida: payload JSON inesperado.")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise SigenergyAPIError(sanitize_sigenergy_error(f"{payload.get('msg') or 'Pedido Sigenergy falhou.'} (code={code})"))
    data = payload.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data
    return data


def _auth_headers(region: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Accept": "application/json", "sigen-region": region}


def _bearer_headers(access_token: str, region: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}", "sigen-region": region}


def _cache_key(config: dict[str, Any]) -> str:
    return f"{config.get('base_url', '')}|{config.get('username', '')}|{config.get('region', 'eu')}"


def invalidate_access_token(config: dict[str, Any]) -> None:
    with _TOKEN_LOCK:
        _TOKEN_CACHE.pop(_cache_key(config), None)


def authenticate(config: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    app_key = str(config.get("username") or "").strip()
    app_secret = str(config.get("password") or "").strip()
    if not app_key or not app_secret:
        raise SigenergyAPIError("Preenche App Key e App Secret da Sigenergy.")

    http = session or requests.Session()
    encoded_key = base64.b64encode(f"{app_key}:{app_secret}".encode("utf-8")).decode("ascii")
    response = http.post(
        build_sigenergy_url(str(config.get("base_url") or ""), str(config.get("login_endpoint") or "")),
        json={"key": encoded_key},
        headers=_auth_headers(str(config.get("region") or "eu")),
        timeout=30,
    )
    response.raise_for_status()
    data = parse_sigenergy_response(response.json())
    if not isinstance(data, dict):
        raise SigenergyAPIError("A resposta Sigenergy de login nao trouxe data JSON valido.")
    access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not access_token:
        raise SigenergyAPIError("A resposta Sigenergy de login nao trouxe accessToken.")
    return data


def get_access_token(
    config: dict[str, Any],
    session: requests.Session | None = None,
    *,
    force_login: bool = False,
) -> str:
    now = datetime.now()
    key = _cache_key(config)
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if cached and not force_login and cached["expires_at"] > now:
            return str(cached["access_token"])

    data = authenticate(config, session=session)
    access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    raw_ttl = data.get("expiresIn") or data.get("expires_in") or data.get("expires")
    try:
        ttl_seconds = int(float(str(raw_ttl))) if raw_ttl not in (None, "") else DEFAULT_TOKEN_TTL_SECONDS
    except (TypeError, ValueError):
        ttl_seconds = DEFAULT_TOKEN_TTL_SECONDS
    expires_at = now + timedelta(seconds=max(min(ttl_seconds, DEFAULT_TOKEN_TTL_SECONDS) - 60, 300))
    with _TOKEN_LOCK:
        _TOKEN_CACHE[key] = {"access_token": access_token, "expires_at": expires_at}
    return access_token


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


class SigenergyClient:
    def __init__(self, config: dict[str, Any], session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def authenticate(self) -> str:
        return get_access_token(self.config, session=self.session, force_login=True)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Any | None = None,
        validate_code: bool = True,
        refreshed: bool = False,
    ) -> dict[str, Any]:
        token = get_access_token(self.config, session=self.session)
        response = self.session.request(
            method,
            build_sigenergy_url(str(self.config.get("base_url") or ""), endpoint),
            headers=_bearer_headers(token, str(self.config.get("region") or "eu")),
            json=json_payload,
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401 and not refreshed:
                invalidate_access_token(self.config)
                get_access_token(self.config, session=self.session, force_login=True)
                return self._request(method, endpoint, json_payload=json_payload, validate_code=validate_code, refreshed=True)
            raise SigenergyAPIError(sanitize_sigenergy_error(exc)) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise SigenergyAPIError("Resposta Sigenergy invalida: payload JSON inesperado.")
        if validate_code:
            parse_sigenergy_response(payload)
        return payload

    def list_systems(self) -> list[dict[str, Any]]:
        configured_ids = str(self.config.get("system_ids") or "").strip()
        if configured_ids:
            return [{"systemId": item, "systemName": item} for item in re.split(r"[,;\s]+", configured_ids) if item]
        payload = self._request("GET", str(self.config.get("systems_endpoint") or self.config.get("plants_endpoint") or ""))
        rows = _rows_from_data(parse_sigenergy_response(payload))
        if not rows:
            raise SigenergyAPIError(
                "A API Sigenergy respondeu com sucesso, mas sem sistemas. Confirma se a App Key tem sistemas autorizados."
            )
        return rows

    def get_energy_flow(self, system_id: str) -> dict[str, Any]:
        endpoint = str(self.config.get("energy_flow_endpoint") or "").replace("{systemId}", "{system_id}")
        endpoint = endpoint.replace("{system_id}", str(system_id))
        payload = self._request("GET", endpoint)
        data = parse_sigenergy_response(payload)
        return data if isinstance(data, dict) else {"raw_data": data}

    def onboard_system(self, system_id: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            str(self.config.get("onboard_endpoint") or "/openapi/board/onboard"),
            json_payload=[system_id],
            validate_code=False,
        )
        return normalize_onboarding_response(system_id, payload)


def list_systems(config: dict[str, Any], session: requests.Session | None = None) -> list[dict[str, Any]]:
    return SigenergyClient(config, session=session).list_systems()


def get_energy_flow(config: dict[str, Any], system_id: str, session: requests.Session | None = None) -> dict[str, Any]:
    return SigenergyClient(config, session=session).get_energy_flow(system_id)


def normalize_onboarding_response(system_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code")
    message = sanitize_sigenergy_error(payload.get("msg") or payload.get("message") or "")
    provider_code = "" if code is None else str(code)
    if code in (0, "0", None):
        status = "requested"
    elif provider_code == "1401":
        status = "already_requested_or_onboarded"
    else:
        status = "failed"
    return {
        "system_id": system_id,
        "status": status,
        "provider_code": provider_code,
        "message": message,
        "response": sanitize_payload(payload),
    }


def onboard_system(config: dict[str, Any], system_id: str, session: requests.Session | None = None) -> dict[str, Any]:
    return SigenergyClient(config, session=session).onboard_system(system_id)


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def map_sigenergy_status(raw_status: Any) -> str:
    normalized = " ".join(str(raw_status or "").strip().lower().replace("_", " ").replace("-", " ").split())
    if normalized in {"normal", "online", "running"}:
        return "Operacional"
    if normalized in {"fault", "error", "abnormal"}:
        return "Erro"
    if normalized in {"offline", "disconnected"}:
        return "Desconectada"
    return str(raw_status or "").strip().title() or "Sem dados"


def normalize_system(system: dict[str, Any]) -> dict[str, Any]:
    external_id = str(_first(system, ("systemId", "id", "stationId", "plantId")) or "").strip()
    if not external_id:
        raise SigenergyAPIError("A resposta Sigenergy nao trouxe systemId numa das linhas.")
    external_name = str(_first(system, ("systemName", "name", "stationName", "plantName")) or external_id).strip()
    raw_status = _first(system, ("status", "systemStatus", "runningStatus", "state"))
    return {
        "external_id": external_id,
        "external_name": external_name,
        "raw_status": "" if raw_status is None else str(raw_status).strip(),
        "normalized_status": map_sigenergy_status(raw_status),
        "pv_capacity_kw": _float_or_none(system.get("pvCapacity")),
        "battery_capacity_kwh": _float_or_none(system.get("batteryCapacity")),
        "payload": system,
    }


def normalize_energy_flow(flow: dict[str, Any]) -> dict[str, Any]:
    return {
        "pv_power_kw": _float_or_none(flow.get("pvPower")),
        "load_power_kw": _float_or_none(flow.get("loadPower")),
        "grid_power_kw_raw": _float_or_none(flow.get("gridPower")),
        "battery_power_kw": _float_or_none(flow.get("batteryPower")),
        "battery_soc_pct": _float_or_none(flow.get("batterySoc")),
        "ev_power_kw": _float_or_none(flow.get("evPower")),
        "ac_power_kw": _float_or_none(flow.get("acPower")),
        "heat_pump_power_kw": _float_or_none(flow.get("heatPumpPower")),
        "payload": flow,
    }
