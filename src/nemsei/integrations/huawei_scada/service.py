"""What an operator has verified about one Huawei SCADA connection.

Every value here is required rather than defaulted, for the reason the
FusionSolar and Sigenergy contracts are: this provider hands over raw
registers with no units, no signs and no documentation, and a plausible guess
produces a plausible-looking number that is wrong. A refusal to run is
recoverable; a customer report built on an inverted grid sign is not.

Three things are verified separately because they can be verified separately:

* **the power unit** -- the registers are integers with gain 1000, which makes
  them watts and the scaled value kilowatts. Cheap to confirm against the
  installation's own inverter display, so it is confirmed, not assumed.
* **which register is production** -- 37498 is total input (PV side) and 37516
  is total active power (AC side). Nothing in the observed traffic says which
  one this account's reports should call production, and the two differ by
  conversion losses. There is no default.
* **the grid sign convention** -- 37502 is signed, and nothing observed says
  whether positive means importing or exporting. Without this stated,
  `rollup.py` writes no `export_energy` and no `grid_import_energy` at all,
  rather than picking one and being wrong half the time.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import ProviderConnection
from nemsei.providers.registry import ProviderCode

_REFERENCE = re.compile(r"^[A-Za-z0-9_]{1,80}$")

PRODUCTION_SIGNALS = ("pv_input_power", "total_active_power")
GRID_SIGN_CONVENTIONS = ("positive_import", "positive_export")
SELF_USE_DERIVATIONS = ("none", "consumption_minus_grid_import")
DEFAULT_MAX_SAMPLE_GAP_SECONDS = 900


class HuaweiScadaConfigurationError(Exception):
    """A configuration refusal, carrying the provider-neutral error with it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error = ProviderError(ProviderErrorCode.CONFIGURATION, message)


@dataclass(frozen=True)
class HuaweiScadaContract:
    """The operator-verified meaning of this connection's registers."""

    credential_reference: str
    power_unit: str
    production_signal: str
    grid_sign_convention: str | None
    self_use_derivation: str
    max_sample_gap_seconds: int

    @property
    def grid_export_is_positive(self) -> bool:
        if self.grid_sign_convention is None:
            raise HuaweiScadaConfigurationError(
                "Huawei SCADA grid sign convention is not verified for this connection."
            )
        return self.grid_sign_convention == "positive_export"

    @property
    def derives_grid_flows(self) -> bool:
        return self.grid_sign_convention is not None

    @property
    def derives_self_use(self) -> bool:
        return self.self_use_derivation == "consumption_minus_grid_import" and self.derives_grid_flows


def _prefix(connection: ProviderConnection) -> str:
    reference = connection.credential_reference or ""
    if not _REFERENCE.fullmatch(reference):
        raise HuaweiScadaConfigurationError("Huawei SCADA credential reference is not configured.")
    return f"NEMSEI_V2_HUAWEI_SCADA_{reference.upper()}"


def _read(prefix: str, name: str) -> str:
    return os.environ.get(f"{prefix}_{name}", "").strip()


def require_huawei_scada_connection(connection: ProviderConnection | None) -> ProviderConnection:
    if connection is None:
        raise HuaweiScadaConfigurationError("Unknown provider connection.")
    if connection.provider_code != ProviderCode.HUAWEI_SCADA.value:
        raise HuaweiScadaConfigurationError("Connection is not Huawei SCADA.")
    if not connection.enabled or connection.configuration_status != "configured":
        raise HuaweiScadaConfigurationError("Huawei SCADA connection is not enabled and configured.")
    return connection


def contract_for(connection: ProviderConnection) -> HuaweiScadaContract:
    """Read the verified contract, or refuse. There is no partial acceptance."""
    prefix = _prefix(connection)
    power_unit = _read(prefix, "POWER_UNIT")
    if power_unit.casefold() != "kw":
        raise HuaweiScadaConfigurationError(
            "Huawei SCADA power unit must be verified as kW (register gain 1000 over watts)."
        )
    production_signal = _read(prefix, "PRODUCTION_SIGNAL")
    if production_signal not in PRODUCTION_SIGNALS:
        raise HuaweiScadaConfigurationError(
            "Huawei SCADA production signal must be verified as one of "
            f"{', '.join(PRODUCTION_SIGNALS)}; register 37498 and register 37516 are not the same measurement."
        )
    grid_sign = _read(prefix, "GRID_SIGN_CONVENTION") or None
    if grid_sign is not None and grid_sign not in GRID_SIGN_CONVENTIONS:
        raise HuaweiScadaConfigurationError(
            f"Huawei SCADA grid sign convention must be one of {', '.join(GRID_SIGN_CONVENTIONS)}."
        )
    self_use = _read(prefix, "SELF_USE_DERIVATION") or "none"
    if self_use not in SELF_USE_DERIVATIONS:
        raise HuaweiScadaConfigurationError(
            f"Huawei SCADA self-use derivation must be one of {', '.join(SELF_USE_DERIVATIONS)}."
        )
    if self_use != "none" and grid_sign is None:
        raise HuaweiScadaConfigurationError(
            "Deriving self-use requires a verified grid sign convention; it is computed from grid import."
        )
    gap = _read(prefix, "MAX_SAMPLE_GAP_SECONDS")
    try:
        max_gap = int(gap) if gap else DEFAULT_MAX_SAMPLE_GAP_SECONDS
    except ValueError as exc:
        raise HuaweiScadaConfigurationError("Huawei SCADA maximum sample gap must be an integer number of seconds.") from exc
    if max_gap <= 0:
        raise HuaweiScadaConfigurationError("Huawei SCADA maximum sample gap must be positive.")
    return HuaweiScadaContract(
        credential_reference=connection.credential_reference or "",
        power_unit="kW",
        production_signal=production_signal,
        grid_sign_convention=grid_sign,
        self_use_derivation=self_use,
        max_sample_gap_seconds=max_gap,
    )


def peer_fingerprint(peer_host: str, *, salt: str) -> str:
    """A stable, salted digest of a peer address -- never the address itself.

    Salted with the deployment's own secret so the digest is meaningless
    outside this installation, and truncated because 128 bits is far more than
    enough to tell two customers' routers apart. Nothing reverses this into an
    address, which is deliberate: the mapping decision must come from the
    dongle serial an operator approved, never from where a packet arrived
    from.
    """
    digest = hmac.new(salt.encode("utf-8"), peer_host.strip().encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]
