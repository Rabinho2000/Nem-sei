from __future__ import annotations

from nemsei.app import create_app
from nemsei.web.db_session import get_request_session


class RecordingSession:
    def __init__(self, *, in_transaction: bool) -> None:
        self._in_transaction = in_transaction
        self.rollback_calls = 0
        self.close_calls = 0

    def in_transaction(self) -> bool:
        return self._in_transaction

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def test_request_session_is_lazy_and_closed_after_success(settings) -> None:
    app = create_app(settings)
    created: list[RecordingSession] = []
    app.extensions["nemsei.session_factory"] = lambda: created.append(RecordingSession(in_transaction=False)) or created[-1]
    with app.test_request_context("/"):
        assert created == []
        assert get_request_session() is created[0]
    assert created[0].rollback_calls == 0
    assert created[0].close_calls == 1


def test_request_session_rolls_back_and_closes_after_exception(settings) -> None:
    app = create_app(settings)
    created: list[RecordingSession] = []
    app.extensions["nemsei.session_factory"] = lambda: created.append(RecordingSession(in_transaction=True)) or created[-1]
    try:
        with app.test_request_context("/"):
            get_request_session()
            raise RuntimeError("route failed")
    except RuntimeError:
        pass
    assert created[0].rollback_calls == 1
    assert created[0].close_calls == 1
