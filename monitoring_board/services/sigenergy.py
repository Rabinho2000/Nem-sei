from __future__ import annotations

from typing import Any

import requests

from monitoring_board.services.sigenergy_client import (
    SigenergyClient,
    authenticate,
    build_sigenergy_url,
    clear_token_cache_for_tests,
    get_access_token,
    get_energy_flow,
    invalidate_access_token,
    list_systems,
)
from monitoring_board.services.sigenergy_errors import SigenergyApiError
from monitoring_board.services.sigenergy_models import (
    first as _first,
    float_or_none as _float_or_none,
    map_sigenergy_status,
    normalize_energy_flow,
    normalize_system,
    parse_sigenergy_response,
    rows_from_data as _rows_from_data,
    sanitize_payload,
    sanitize_sigenergy_error,
)


SigenergyAPIError = SigenergyApiError


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
    client = SigenergyClient(config, session=session)
    payload = client.request_json(
        "POST",
        str(config.get("onboard_endpoint") or "/openapi/board/onboard"),
        json_payload=[system_id],
        validate_code=False,
    )
    return normalize_onboarding_response(system_id, payload)
