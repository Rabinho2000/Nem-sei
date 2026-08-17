"""Sigenergy credential-reference resolution and endpoint configuration."""
from __future__ import annotations

import os
import re

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
    region = os.environ.get(f"{prefix}_REGION", "").strip()
    if not all((app_key, app_secret, base_url, auth_endpoint, systems_endpoint, energy_flow_endpoint, region)):
        raise SigenergyClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Sigenergy credentials or endpoints are not configured."))
    endpoints = SigenergyEndpoints(base_url, auth_endpoint, systems_endpoint, energy_flow_endpoint, region)
    return SigenergyCredentials(app_key, app_secret), endpoints
