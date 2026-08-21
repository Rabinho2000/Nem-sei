"""A delivery interface for Telegram, and a mock -- no real client yet.

D3 (`docs/v2/DIAGNOSTICS_PORTFOLIO_TELEGRAM_PLAN.md`): "não envies mensagens
reais para Telegram" is not a runtime flag someone could get wrong -- there
is no code in this module capable of an HTTP call at all. A real
`requests.post(...)`-backed implementation is D4's job, gated on separate,
explicit approval, and it would live here, behind the same
`TelegramClient` protocol, so `notifications/service.py` never has to change
to start using it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


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
