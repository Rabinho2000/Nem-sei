from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from monitoring_board.services.sigenergy_errors import SigenergyApiError
from monitoring_board.services.sigenergy_models import SigenergyEndpoints


EXPERTCOM_SIGENERGY_BASE_URL = "https://api-eu.sigencloud.com"
EXPERTCOM_SIGENERGY_SYSTEM_ID = "TZXRS1780315946"


@dataclass(frozen=True)
class SigenergyPreviewReadOnlyPolicy:
    """Exact outbound allowlist for the one-shot Expertcom preview worker."""

    base_url: str = EXPERTCOM_SIGENERGY_BASE_URL
    system_id: str = EXPERTCOM_SIGENERGY_SYSTEM_ID

    def validate_endpoints(self, endpoints: SigenergyEndpoints) -> None:
        expected = {
            "base_url": self.base_url,
            "login_endpoint": "/openapi/auth/login/key",
            "systems_endpoint": "/openapi/system",
            "energy_flow_endpoint": (
                f"/openapi/systems/{self.system_id}/energyFlow"
            ),
            "history_endpoint": f"/openapi/systems/{self.system_id}/history",
            "region": "eu",
        }
        actual = {
            "base_url": endpoints.base_url.rstrip("/"),
            "login_endpoint": self._path(endpoints.login_endpoint),
            "systems_endpoint": self._path(endpoints.systems_endpoint),
            "energy_flow_endpoint": self._path(
                endpoints.energy_flow_endpoint.replace(
                    "{systemId}", self.system_id
                ).replace("{system_id}", self.system_id)
            ),
            "history_endpoint": self._path(
                endpoints.history_endpoint.replace(
                    "{systemId}", self.system_id
                ).replace("{system_id}", self.system_id)
            ),
            "region": endpoints.region,
        }
        if actual != expected:
            raise SigenergyApiError(
                "A configuracao do worker Sigenergy preview nao corresponde "
                "a allowlist read-only da Expertcom."
            )

    def authorize_login(self, endpoint: str) -> None:
        if self._path(endpoint) != "/openapi/auth/login/key":
            raise SigenergyApiError(
                "Endpoint Sigenergy recusado pela allowlist read-only da preview."
            )

    def authorize_request(self, method: str, endpoint: str) -> None:
        allowed = {
            "/openapi/system",
            f"/openapi/systems/{self.system_id}/energyFlow",
            f"/openapi/systems/{self.system_id}/history",
        }
        if method.upper() != "GET" or self._path(endpoint) not in allowed:
            raise SigenergyApiError(
                "Endpoint Sigenergy recusado pela allowlist read-only da preview."
            )

    def authorize_system_id(self, system_id: str) -> None:
        if str(system_id).strip() != self.system_id:
            raise SigenergyApiError(
                "System ID Sigenergy recusado pela allowlist read-only da preview."
            )

    def filter_discovered_systems(
        self,
        systems: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        allowed: list[dict[str, Any]] = []
        for row in systems:
            returned_id = str(
                row.get("systemId")
                or row.get("id")
                or row.get("stationId")
                or row.get("plantId")
                or ""
            ).strip()
            if returned_id == self.system_id:
                allowed.append(row)
        return allowed

    @staticmethod
    def _path(endpoint: str) -> str:
        return "/" + str(endpoint or "").strip().lstrip("/")
