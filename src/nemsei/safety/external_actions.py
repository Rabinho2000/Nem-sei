from __future__ import annotations

from dataclasses import dataclass

from nemsei.config import CAPABILITIES


class ExternalActionDenied(PermissionError):
    """Raised before a provider, notification, or distribution side effect."""


@dataclass(frozen=True)
class CapabilityPolicy:
    values: dict[str, bool]

    def allows(self, capability: str) -> bool:
        if capability not in CAPABILITIES:
            raise ValueError(f"Unknown external capability: {capability}")
        return bool(self.values.get(capability, False))

    def require(self, capability: str) -> None:
        if not self.allows(capability):
            raise ExternalActionDenied(f"V2 external capability is disabled: {capability}")
