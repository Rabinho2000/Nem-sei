from __future__ import annotations

import logging
import os
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)
TELEGRAM_SEND_MESSAGE_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def telegram_alerts_enabled() -> bool:
    return _env_flag("TELEGRAM_ALERTS_ENABLED", False)


def telegram_daily_summary_enabled() -> bool:
    return _env_flag("TELEGRAM_DAILY_SUMMARY_ENABLED", False)


def get_telegram_config() -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return {
        "token": token,
        "chat_id": chat_id,
        "token_configured": bool(token),
        "chat_id_configured": bool(chat_id),
        "alerts_enabled": telegram_alerts_enabled(),
        "daily_summary_enabled": telegram_daily_summary_enabled(),
        "masked_token": mask_secret(token),
    }


def mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + ("*" * max(len(value) - 4, 2)) + value[-2:]
    return value[:4] + ("*" * 10) + value[-6:]


def is_telegram_configured() -> bool:
    config = get_telegram_config()
    return bool(config["token_configured"] and config["chat_id_configured"])


def send_telegram_message(text: str) -> bool:
    config = get_telegram_config()
    token = config["token"]
    chat_id = config["chat_id"]
    if not token or not chat_id:
        LOGGER.info("Telegram message skipped because token or chat id is not configured.")
        return False

    try:
        response = requests.post(
            TELEGRAM_SEND_MESSAGE_URL.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            LOGGER.warning("Telegram API returned a non-ok response: %s", payload)
            return False
        return True
    except requests.RequestException as exc:
        LOGGER.warning("Telegram sendMessage failed: %s", exc)
    except ValueError as exc:
        LOGGER.warning("Telegram response was not valid JSON: %s", exc)
    return False


def test_telegram_connection() -> tuple[bool, str]:
    if not is_telegram_configured():
        return False, "Telegram nao esta configurado: falta token ou chat ID."
    ok = send_telegram_message("<b>Monitoring Board Local</b>\nMensagem de teste Telegram.")
    if ok:
        return True, "Mensagem de teste enviada com sucesso."
    return False, "Falha ao enviar mensagem de teste. Verifica token, chat ID e ligacao."
