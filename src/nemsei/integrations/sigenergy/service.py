"""Sigenergy credential-reference resolution and endpoint configuration."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nemsei.config import ConfigurationError, read_secret_value
from nemsei.integrations.sigenergy.client import SigenergyClientError, SigenergyCredentials, SigenergyEndpoints
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import ProviderConnection

_REFERENCE = re.compile(r"^[A-Za-z0-9_]{1,80}$")


def credentials_for(connection: ProviderConnection) -> tuple[SigenergyCredentials, SigenergyEndpoints]:
    reference = connection.credential_reference or ""
    if not _REFERENCE.fullmatch(reference):
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy credential reference is not configured."))
    prefix = f"NEMSEI_V2_SIGENERGY_{reference.upper()}"
    try:
        app_key = read_secret_value(value_name=f"{prefix}_APP_KEY", file_name=f"{prefix}_APP_KEY_FILE")
        app_secret = read_secret_value(value_name=f"{prefix}_APP_SECRET", file_name=f"{prefix}_APP_SECRET_FILE")
    except ConfigurationError as exc:
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy credential configuration is invalid.")) from exc
    base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip()
    auth_endpoint = os.environ.get(f"{prefix}_AUTH_ENDPOINT", "").strip()
    systems_endpoint = os.environ.get(f"{prefix}_SYSTEMS_ENDPOINT", "").strip()
    energy_flow_endpoint = os.environ.get(f"{prefix}_ENERGY_FLOW_ENDPOINT", "").strip()
    history_endpoint = os.environ.get(f"{prefix}_HISTORY_ENDPOINT", "").strip() or "/openapi/systems/{system_id}/history"
    region = os.environ.get(f"{prefix}_REGION", "").strip()
    if not all((app_key, app_secret, base_url, auth_endpoint, systems_endpoint, energy_flow_endpoint, region)):
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy credentials or endpoints are not configured."))
    endpoints = SigenergyEndpoints(base_url, auth_endpoint, systems_endpoint, energy_flow_endpoint, region, history_endpoint)
    return SigenergyCredentials(app_key, app_secret), endpoints


@dataclass(frozen=True)
class SigenergyProductionContract:
    """What an operator has verified about this account's daily history.

    Neither value is inferred, and neither has a default. V1 passes
    `date.today()` straight to the provider and accepts whatever comes back,
    which assumes the provider's day is the server's day -- an assumption that
    has never been checked. Requiring the timezone explicitly turns a silent
    wrong answer into a refusal to run, which is the same bar FusionSolar's
    `PVYield` contract had to clear.

    The unit is required for the same reason V1 requires it: the history
    payload does not carry a reliable unit in every response, so a value is
    only trustworthy when the payload says kWh or an operator has confirmed it
    for this account.
    """

    source_timezone: ZoneInfo
    source_timezone_name: str
    canonical_unit: str


def production_contract_for(connection: ProviderConnection) -> SigenergyProductionContract:
    reference = connection.credential_reference or ""
    if not _REFERENCE.fullmatch(reference):
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy credential reference is not configured."))
    prefix = f"NEMSEI_V2_SIGENERGY_{reference.upper()}"
    timezone_name = os.environ.get(f"{prefix}_PRODUCTION_TIMEZONE", "").strip()
    unit = os.environ.get(f"{prefix}_PRODUCTION_UNIT", "").strip()
    if not timezone_name or not unit:
        raise SigenergyClientError(
            ProviderError(
                ProviderErrorCode.CONFIGURATION,
                "Sigenergy production history needs an operator-verified source timezone and unit.",
            )
        )
    if unit.casefold() != "kwh":
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy production unit must be verified as kWh."))
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy production timezone is not a valid IANA identifier.")) from exc
    return SigenergyProductionContract(zone, timezone_name, "kWh")
