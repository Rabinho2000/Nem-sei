from __future__ import annotations

import re
from typing import Any


DEFAULT_SYNC_HOURS = "08:00,14:00"


def normalize_sync_hours(raw_value: str) -> str:
    candidates = [item.strip() for item in raw_value.split(",") if item.strip()]
    normalized: list[str] = []
    for item in candidates[:2]:
        if re.fullmatch(r"\d{2}:\d{2}", item):
            hour, minute = item.split(":")
            if 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59:
                normalized.append(item)
    if not normalized:
        normalized = DEFAULT_SYNC_HOURS.split(",")
    if len(normalized) == 1:
        normalized.append("14:00" if normalized[0] != "14:00" else "08:00")
    return ",".join(normalized[:2])


def build_provider_url(base_url: str, endpoint: str) -> str:
    if not base_url or not endpoint:
        raise ValueError("Configura a base URL e os endpoints da API FusionSolar antes de sincronizar.")
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def map_fusionsolar_status(raw_status: Any) -> str:
    raw_value = "" if raw_status is None else str(raw_status).strip()
    if raw_value in {"1", "1.0"}:
        return "Desconectada"
    if raw_value in {"2", "2.0"}:
        return "Erro"
    if raw_value in {"3", "3.0"}:
        return "Operacional"

    normalized = normalize_name(raw_value)
    if normalized in {"fault", "alarm", "error", "critical", "faulty"}:
        return "Erro"
    if normalized in {"offline", "disconnected", "no signal", "communication lost"}:
        return "Desconectada"
    if normalized in {"running", "normal", "online", "ok", "healthy"}:
        return "Operacional"
    return normalize_status(raw_value or "Operacional")


def describe_fusionsolar_health_state(raw_status: Any) -> str:
    raw_value = "" if raw_status is None else str(raw_status).strip()
    if raw_value in {"1", "1.0"}:
        return "disconnected"
    if raw_value in {"2", "2.0"}:
        return "faulty"
    if raw_value in {"3", "3.0"}:
        return "healthy"
    return raw_value or "unknown"


def normalize_name(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", ascii_value.strip().lower())


def normalize_status(value: str) -> str:
    normalized = normalize_name(value)
    if not normalized:
        return "Desconectada"
    if normalized in {"erro", "error", "falha", "avaria", "down", "offline", "fault"}:
        return "Erro"
    if normalized in {"desconectada", "desconectado", "sem comunicacao", "offline", "desligada"}:
        return "Desconectada"
    if normalized in {"ok", "operacional", "online", "normal", "sem erro"}:
        return "Operacional"
    if "descon" in normalized or "offline" in normalized:
        return "Desconectada"
    if "erro" in normalized or "fault" in normalized or "falha" in normalized or "alarm" in normalized:
        return "Erro"
    return value.strip().title() or "Operacional"
