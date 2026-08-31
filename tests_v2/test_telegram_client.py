"""D4: the real Telegram client, and the guarantees that survived building it.

D3 shipped this module with no code capable of an HTTP call so that "no real
messages" was structural. Building the client removes that particular
guarantee, so the ones that replace it are tested here: no token means no
sending, and the token never reaches an error string.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from nemsei.notifications.telegram_client import (
    MAX_MESSAGE_CHARS,
    HttpTelegramClient,
    MockTelegramClient,
    default_client_factory,
)


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


def capture(monkeypatch, *, payload: bytes = b'{"ok": true}', raises: Exception | None = None) -> dict:
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        if raises is not None:
            raise raises
        return FakeResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_a_client_cannot_exist_without_a_token() -> None:
    with pytest.raises(ValueError, match="requires a bot token"):
        HttpTelegramClient(bot_token="   ")


def test_the_factory_falls_back_to_the_mock_when_no_token_is_configured(monkeypatch) -> None:
    # The safety property that replaces D3's "no HTTP code exists": a
    # deployment with no token cannot send, whatever is switched on in the UI.
    # The capability is on here so that the token, and only the token, is what
    # this test is about.
    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "true")
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN_FILE", raising=False)

    assert isinstance(default_client_factory(None), MockTelegramClient)


def test_the_factory_builds_a_real_client_once_a_token_exists(monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "true")
    monkeypatch.setenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN_FILE", raising=False)

    assert isinstance(default_client_factory(None), HttpTelegramClient)


def test_a_token_placed_in_a_mounted_file_is_read(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "true")
    secret = tmp_path / "telegram_token"
    secret.write_text("999:zzz\n", encoding="utf-8")
    monkeypatch.delenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN_FILE", str(secret))

    client = default_client_factory(None)
    assert isinstance(client, HttpTelegramClient)
    assert client.bot_token == "999:zzz"


def test_a_successful_send_posts_the_message_to_the_chat(monkeypatch) -> None:
    seen = capture(monkeypatch)

    result = HttpTelegramClient(bot_token="123:abc").send_message(chat_id="-100", text="olá")

    assert result.delivered is True
    assert seen["body"]["chat_id"] == "-100"
    assert seen["body"]["text"] == "olá"
    assert seen["url"].endswith("/bot123:abc/sendMessage")


def test_the_token_never_reaches_an_error_string(monkeypatch) -> None:
    # The token is a path segment of the URL, so any error text echoing the URL
    # would leak it into notification_events.error and onto an operator screen.
    error = urllib.error.HTTPError(
        url="https://api.telegram.org/botSUPER-SECRET/sendMessage", code=400,
        msg="Bad Request", hdrs=None, fp=None,
    )
    error.read = lambda: json.dumps({"description": "chat not found"}).encode("utf-8")
    capture(monkeypatch, raises=error)

    result = HttpTelegramClient(bot_token="SUPER-SECRET").send_message(chat_id="-100", text="x")

    assert result.delivered is False
    assert "SUPER-SECRET" not in (result.error or "")
    assert "chat not found" in result.error


def test_telegram_refusing_the_message_is_a_failure_not_a_success(monkeypatch) -> None:
    capture(monkeypatch, payload=json.dumps({"ok": False, "description": "bot was blocked"}).encode("utf-8"))

    result = HttpTelegramClient(bot_token="123:abc").send_message(chat_id="-100", text="x")

    assert result.delivered is False
    assert "bot was blocked" in result.error


def test_an_unreachable_provider_is_reported_without_a_traceback(monkeypatch) -> None:
    capture(monkeypatch, raises=urllib.error.URLError("no route"))

    result = HttpTelegramClient(bot_token="123:abc").send_message(chat_id="-100", text="x")

    assert result.delivered is False
    assert "unreachable" in result.error


def test_an_oversized_message_is_truncated_rather_than_rejected_forever(monkeypatch) -> None:
    # Telegram rejects over 4096 chars outright; a digest that grew past it
    # would fail every retry identically.
    seen = capture(monkeypatch)

    HttpTelegramClient(bot_token="123:abc").send_message(chat_id="-100", text="x" * (MAX_MESSAGE_CHARS + 500))

    assert len(seen["body"]["text"]) <= MAX_MESSAGE_CHARS
    assert seen["body"]["text"].endswith("truncada]")


def test_a_channel_without_a_chat_id_fails_before_any_network_call(monkeypatch) -> None:
    seen = capture(monkeypatch)

    result = HttpTelegramClient(bot_token="123:abc").send_message(chat_id="", text="x")

    assert result.delivered is False
    assert seen == {}


def test_the_global_kill_switch_leaves_no_client_that_can_reach_the_network(monkeypatch) -> None:
    """`NEMSEI_V2_NOTIFICATIONS=false` with a token mounted and a channel enabled.

    This was the live state on 2026-08-31: the switch said false, the token was
    mounted, the channel was on, and real Telegram messages were being
    delivered -- because nothing anywhere read the switch. The factory now
    refuses to build a sender at all, and the refusal raises rather than
    pretending to have sent or pretending to have failed.
    """
    from nemsei.notifications.telegram_client import DeniedTelegramClient
    from nemsei.safety.external_actions import ExternalActionDenied

    monkeypatch.setenv("NEMSEI_V2_NOTIFICATIONS", "false")
    monkeypatch.setenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", "123:abc")

    client = default_client_factory(None)
    assert isinstance(client, DeniedTelegramClient)
    with pytest.raises(ExternalActionDenied, match="notifications"):
        client.send_message(chat_id="chat-1", text="should never leave this process")


def test_an_unset_kill_switch_is_off_not_on(monkeypatch) -> None:
    """Default-deny, matching safety/external_actions.py."""
    from nemsei.notifications.telegram_client import DeniedTelegramClient

    monkeypatch.delenv("NEMSEI_V2_NOTIFICATIONS", raising=False)
    monkeypatch.setenv("NEMSEI_V2_TELEGRAM_BOT_TOKEN", "123:abc")
    assert isinstance(default_client_factory(None), DeniedTelegramClient)
