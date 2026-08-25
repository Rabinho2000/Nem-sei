"""Telegram delivery: the interface, the mock, and now a real HTTP client.

D3 shipped this module with no code capable of an HTTP call, deliberately, so
that "no real messages" was structural rather than a flag someone could get
wrong. D4 -- building the real client -- was gated on explicit human approval,
which was given on 2026-08-25.

The structural guarantee is preserved rather than removed. `HttpTelegramClient`
cannot be constructed without a bot token, and the factory in
`notifications/service.py` falls back to the mock when no token is configured,
so an unconfigured deployment still cannot send anything. The kill switch that
matters most is still `NotificationChannel.enabled`, which stops the delivery
step before any client is built at all.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

TELEGRAM_API_BASE = "https://api.telegram.org"
# Telegram rejects messages over 4096 characters outright. A digest that grew
# past it would fail every retry identically, so it is cut here with a marker
# rather than lost to a 400 nobody can act on.
MAX_MESSAGE_CHARS = 4096
TRUNCATION_MARKER = "\n[…mensagem truncada]"


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    error: str | None = None


class TelegramClient(Protocol):
    """What `notifications/service.py` needs from any delivery mechanism.

    Deliberately the smallest possible surface -- one message, one target,
    one outcome -- so a real implementation (D4) has nothing to get wrong
    beyond the HTTP call itself.
    """

    def send_message(self, *, chat_id: str, text: str) -> DeliveryResult: ...


@dataclass
class MockTelegramClient:
    """Records every call it receives; never makes a network call.

    The only `TelegramClient` this codebase can construct today --
    `notifications/service.py` has no factory path that could produce
    anything else, which is what makes "no real Telegram in D3" a structural
    guarantee rather than a configuration choice.
    """

    # Configurable for tests that need to prove the failure path (proof #8,
    # DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md): a set of chat_ids that should
    # fail delivery, or an explicit callable for more elaborate scenarios.
    fail_for_chat_ids: frozenset[str] = field(default_factory=frozenset)
    sent: list[dict[str, str]] = field(default_factory=list)

    def send_message(self, *, chat_id: str, text: str) -> DeliveryResult:
        self.sent.append({"chat_id": chat_id, "text": text})
        if chat_id in self.fail_for_chat_ids:
            return DeliveryResult(delivered=False, error="mock delivery failure (configured for this test)")
        return DeliveryResult(delivered=True)


@dataclass(frozen=True)
class HttpTelegramClient:
    """Sends through the Telegram Bot API over HTTPS.

    Deliberately narrow: one endpoint, `sendMessage`, and no retry loop of its
    own. Retrying is `notifications/service.py`'s job, which already commits one
    event at a time precisely so a mid-batch crash cannot resend an already
    delivered message -- a retry here would sit inside that transaction and
    could deliver twice while recording once.

    The token never appears in a `DeliveryResult`: it is a path segment of the
    API URL, so any error text that echoed the URL would leak it into
    `notification_events.error` and from there into an operator's screen.
    """

    bot_token: str
    api_base: str = TELEGRAM_API_BASE
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.bot_token.strip():
            raise ValueError("HttpTelegramClient requires a bot token.")

    def send_message(self, *, chat_id: str, text: str) -> DeliveryResult:
        if not chat_id.strip():
            return DeliveryResult(delivered=False, error="no chat_id configured for this channel")
        body = text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
        payload = json.dumps({"chat_id": chat_id, "text": body, "disable_web_page_preview": True}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/bot{self.bot_token}/sendMessage",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return DeliveryResult(delivered=False, error=f"telegram HTTP {exc.code}: {_safe_reason(exc)}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return DeliveryResult(delivered=False, error=f"telegram unreachable: {type(exc).__name__}")
        except json.JSONDecodeError:
            return DeliveryResult(delivered=False, error="telegram returned a response that is not JSON")
        if not decoded.get("ok"):
            return DeliveryResult(delivered=False, error=f"telegram refused: {str(decoded.get('description'))[:180]}")
        return DeliveryResult(delivered=True)


def _safe_reason(exc: urllib.error.HTTPError) -> str:
    """Telegram's own description, never the URL -- which carries the token."""
    try:
        decoded = json.loads(exc.read().decode("utf-8"))
    except (ValueError, OSError):
        return "no description"
    return str(decoded.get("description", "no description"))[:180]


def default_client_factory(channel: object) -> TelegramClient:
    """A real client when a bot token is configured, the mock otherwise.

    The fallback is the safety property, not a convenience: a deployment with
    no token cannot send anything, whatever anyone switches on in the
    interface. The token is read at call time rather than captured at import,
    so rotating the mounted secret takes effect on the next delivery instead of
    the next restart.

    Lives here, and not in `service.py` and `digests.py` separately, so there is
    exactly one place that decides whether this process can reach the network.
    """
    from nemsei.config import read_secret_value

    token = read_secret_value(
        value_name="NEMSEI_V2_TELEGRAM_BOT_TOKEN",
        file_name="NEMSEI_V2_TELEGRAM_BOT_TOKEN_FILE",
    )
    return HttpTelegramClient(bot_token=token) if token else MockTelegramClient()
