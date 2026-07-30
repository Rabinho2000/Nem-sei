from __future__ import annotations

import os


PREVIEW_DISABLED_MESSAGE = (
    "Ações externas desativadas no ambiente PREVIEW / NÃO PRODUÇÃO."
)


class ExternalActionDisabled(RuntimeError):
    """Raised before an external request can leave a preview process."""


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def preview_enabled() -> bool:
    return env_flag("PREVIEW_BANNER", False) or (
        os.environ.get("APP_ENV", "").strip().casefold() == "preview"
    )


def scheduler_enabled() -> bool:
    return env_flag("SCHEDULER_ENABLED", True) and not preview_enabled()


def external_actions_enabled() -> bool:
    return env_flag("EXTERNAL_ACTIONS_ENABLED", True) and not preview_enabled()


def require_external_actions_enabled() -> None:
    if not external_actions_enabled():
        raise ExternalActionDisabled(PREVIEW_DISABLED_MESSAGE)
