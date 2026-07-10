from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from monitoring_board.services.sigenergy_errors import SigenergyApiError


DEFAULT_TOKEN_TTL_SECONDS = 20 * 60


@dataclass(frozen=True)
class SigenergyEndpoints:
    base_url: str
    login_endpoint: str
    systems_endpoint: str
    energy_flow_endpoint: str
    region: str = "eu"


@dataclass(frozen=True)
class SigenergyCredentials:
    app_key: str
    app_secret: str


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


def build_sigenergy_url(base_url: str, endpoint: str, **path_values: str) -> str:
    if not base_url or not endpoint:
        raise SigenergyApiError("Configura a Base URL e os endpoints da API Sigenergy antes de sincronizar.")
    resolved_endpoint = endpoint
    for key, value in path_values.items():
        resolved_endpoint = resolved_endpoint.replace("{" + key + "}", str(value))
    return f"{base_url.rstrip('/')}/{resolved_endpoint.lstrip('/')}"


def parse_sigenergy_response(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise SigenergyApiError("Resposta Sigenergy invalida: payload JSON inesperado.")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise SigenergyApiError(sanitize_sigenergy_error(f"{payload.get('msg') or 'Pedido Sigenergy falhou.'} (code={code})"), payload=payload)
    data = payload.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data
    return data


def rows_from_data(data: Any) -> list[dict[str, Any]]:
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


def first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def float_or_none(value: Any) -> float | None:
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
    external_id = str(first(system, ("systemId", "id", "stationId", "plantId")) or "").strip()
    if not external_id:
        raise SigenergyApiError("A resposta Sigenergy nao trouxe systemId numa das linhas.")
    external_name = str(first(system, ("systemName", "name", "stationName", "plantName")) or external_id).strip()
    raw_status = first(system, ("status", "systemStatus", "runningStatus", "state"))
    return {
        "external_id": external_id,
        "external_name": external_name,
        "raw_status": "" if raw_status is None else str(raw_status).strip(),
        "normalized_status": map_sigenergy_status(raw_status),
        "pv_capacity_kw": float_or_none(system.get("pvCapacity")),
        "battery_capacity_kwh": float_or_none(system.get("batteryCapacity")),
        "payload": system,
    }


def normalize_energy_flow(flow: dict[str, Any]) -> dict[str, Any]:
    return {
        "pv_power_kw": float_or_none(flow.get("pvPower")),
        "load_power_kw": float_or_none(flow.get("loadPower")),
        "grid_power_kw_raw": float_or_none(flow.get("gridPower")),
        "battery_power_kw": float_or_none(flow.get("batteryPower")),
        "battery_soc_pct": float_or_none(flow.get("batterySoc")),
        "ev_power_kw": float_or_none(flow.get("evPower")),
        "ac_power_kw": float_or_none(flow.get("acPower")),
        "heat_pump_power_kw": float_or_none(flow.get("heatPumpPower")),
        "payload": flow,
    }
