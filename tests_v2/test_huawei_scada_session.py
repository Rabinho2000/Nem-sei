"""The dongle conversation, driven against a scripted fake logger.

`FakeDongle` answers Modbus the way the pilot hardware does: it serves the
aggregate block on `unit=100` and refuses `unit=1` with function `0x83`,
exception `0x04`. Everything the listener does to a real logger is exercised
here without a socket, which is the whole reason `session.py` takes an
injected transport.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nemsei.integrations.huawei_scada import protocol as p
from nemsei.integrations.huawei_scada.session import (
    DongleSession,
    SessionClosed,
    SessionProtocolError,
)
from tests_v2.test_huawei_scada_protocol import REAL_ADVERTISEMENT, aggregate_block


class FakeDongle:
    """A logger that answers requests, not a tape of pre-recorded bytes.

    Answering for real matters: a scripted tape would still pass if the
    session sent the wrong transaction id, the wrong unit, or nothing at all.
    """

    def __init__(
        self,
        *,
        banner: bytes = REAL_ADVERTISEMENT,
        block: bytes | None = None,
        refuse_units: dict[int, int] | None = None,
        refuse_block_read: bool = False,
        chunk_size: int | None = None,
        reset_after_requests: int | None = None,
        silent: bool = False,
    ) -> None:
        self.outbox = bytearray(banner)
        self.sent: list[bytes] = []
        self.block = aggregate_block() if block is None else block
        self.refuse_units = {1: 0x04} if refuse_units is None else refuse_units
        self.refuse_block_read = refuse_block_read
        self.chunk_size = chunk_size
        self.reset_after_requests = reset_after_requests
        self.silent = silent
        self.closed = False
        self.timeout: float | None = None
        self.requests = 0

    # --- transport surface --------------------------------------------------

    def settimeout(self, seconds: float | None) -> None:
        self.timeout = seconds

    def close(self) -> None:
        self.closed = True

    def recv(self, size: int) -> bytes:
        if not self.outbox:
            raise TimeoutError("nothing to read")
        take = min(size, self.chunk_size or size, len(self.outbox))
        chunk = bytes(self.outbox[:take])
        del self.outbox[:take]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        self.requests += 1
        if self.reset_after_requests is not None and self.requests > self.reset_after_requests:
            raise ConnectionResetError("dongle went away")
        if self.silent:
            return
        self.outbox.extend(self._answer(data))

    # --- behaviour ----------------------------------------------------------

    def _answer(self, request: bytes) -> bytes:
        transaction_id = int.from_bytes(request[0:2], "big")
        unit_id = request[6]
        function = request[7]
        if unit_id in self.refuse_units:
            return self._exception(transaction_id, unit_id, function, self.refuse_units[unit_id])
        if function == p.FUNCTION_READ_DEVICE_IDENTIFICATION:
            header = bytes([p.MEI_TYPE_DEVICE_IDENTIFICATION, 0x03, 0x01, 0x00, 0x00, 0x01])
            objects = bytes([0x87, 0x0C]) + b"HV2340123456"
            return self._frame(transaction_id, unit_id, bytes([function]) + header + objects)
        address = int.from_bytes(request[8:10], "big")
        quantity = int.from_bytes(request[10:12], "big")
        if address == p.AGGREGATE_BLOCK_START and quantity == p.AGGREGATE_BLOCK_QUANTITY:
            if self.refuse_block_read:
                return self._exception(transaction_id, unit_id, function, 0x03)
            body = self.block
        elif address == p.REGISTER_DONGLE_SOFTWARE:
            body = b"V100R001C00SPC124".ljust(quantity * 2, b"\x00")
        else:
            offset = (address - p.AGGREGATE_BLOCK_START) * 2
            body = self.block[offset : offset + quantity * 2]
        return self._frame(transaction_id, unit_id, bytes([function, len(body)]) + body)

    @staticmethod
    def _frame(transaction_id: int, unit_id: int, pdu: bytes) -> bytes:
        return (
            transaction_id.to_bytes(2, "big")
            + b"\x00\x00"
            + (len(pdu) + 1).to_bytes(2, "big")
            + bytes([unit_id])
            + pdu
        )

    def _exception(self, transaction_id: int, unit_id: int, function: int, code: int) -> bytes:
        return self._frame(transaction_id, unit_id, bytes([function | p.EXCEPTION_FLAG, code]))


def ticking_clock(step_seconds: float = 1.0):
    """A clock that advances on every read, so timeouts expire deterministically."""
    state = {"now": datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)}

    def clock() -> datetime:
        state["now"] += timedelta(seconds=step_seconds)
        return state["now"]

    return clock


def connected(**kwargs) -> tuple[DongleSession, FakeDongle]:
    dongle = FakeDongle(**kwargs)
    session = DongleSession(
        transport=dongle, read_timeout_seconds=15, handshake_timeout_seconds=30, banner_settle_seconds=1
    )
    return session, dongle


# --- handshake ---------------------------------------------------------------


def test_handshake_reads_the_banner_and_adopts_the_announced_unit() -> None:
    session, _dongle = connected()
    advertisement = session.handshake()
    assert advertisement.serial == "HV2340123456"
    assert session.unit_id == 100


def test_handshake_survives_a_banner_delivered_one_byte_at_a_time() -> None:
    session, _dongle = connected(chunk_size=1)
    assert session.handshake().serial == "HV2340123456"


def test_a_peer_that_says_nothing_times_out_rather_than_being_guessed_at() -> None:
    dongle = FakeDongle(banner=b"")
    session = DongleSession(transport=dongle, handshake_timeout_seconds=5, clock=ticking_clock())
    with pytest.raises(TimeoutError):
        session.handshake()


def test_a_peer_that_floods_without_announcing_itself_is_refused() -> None:
    dongle = FakeDongle(banner=b"x" * 9000)
    session = DongleSession(transport=dongle, handshake_timeout_seconds=300)
    with pytest.raises(SessionProtocolError, match="handshake budget"):
        session.handshake()


# --- polling -----------------------------------------------------------------


def test_one_poll_reads_all_five_signals_in_a_single_request() -> None:
    session, dongle = connected()
    session.handshake()
    before = len(dongle.sent)
    outcome = session.poll_aggregate()
    assert outcome.ok
    assert len(dongle.sent) - before == 1
    assert outcome.reading.total_active_power_kw == Decimal("5.400")
    assert outcome.reading.grid_power_kw == Decimal("-2.312")


def test_the_request_asks_the_unit_the_dongle_announced() -> None:
    session, dongle = connected(banner=REAL_ADVERTISEMENT.replace(b"5=100", b"5=42"))
    session.handshake()
    session.poll_aggregate()
    assert dongle.sent[-1][6] == 42


def test_a_dongle_that_refuses_the_wide_read_falls_back_to_register_pairs() -> None:
    session, dongle = connected(refuse_block_read=True)
    session.handshake()
    outcome = session.poll_aggregate()
    assert outcome.ok
    assert outcome.reading.pv_input_power_kw == Decimal("5.432")
    assert "fell back" in (outcome.safe_detail or "")
    assert not session.block_read_supported
    # The fallback is remembered: the next poll does not retry the wide read.
    sent_before = len(dongle.sent)
    assert session.poll_aggregate().ok
    assert len(dongle.sent) - sent_before == len(p.AGGREGATE_SIGNALS)


def test_a_poll_that_gets_no_answer_reports_a_timeout_instead_of_raising() -> None:
    dongle = FakeDongle(banner=b"", silent=True)
    session = DongleSession(transport=dongle, read_timeout_seconds=5, clock=ticking_clock())
    outcome = session.poll_aggregate()
    assert not outcome.ok and outcome.error_code == "timeout"


def test_an_exception_answer_is_reported_by_name_and_does_not_raise() -> None:
    session, _dongle = connected(refuse_units={100: 0x06})
    session.handshake()
    outcome = session.poll_aggregate()
    assert not outcome.ok
    assert outcome.error_code == "modbus_exception"
    assert outcome.exception_name == "slave_device_busy"


def test_transaction_ids_advance_so_a_stale_answer_cannot_be_accepted() -> None:
    session, dongle = connected()
    session.handshake()
    session.poll_aggregate()
    session.poll_aggregate()
    ids = [int.from_bytes(frame[0:2], "big") for frame in dongle.sent]
    assert len(set(ids)) == len(ids)


def test_a_leftover_answer_from_a_previous_request_is_discarded_not_used() -> None:
    """The exact hazard `_await_frame` exists for.

    A response that timed out and arrived late carries an old transaction id.
    Accepting it would stamp an old measurement with the present instant.
    """
    session, dongle = connected()
    session.handshake()
    stale = dongle._frame(999, 100, bytes([0x03, len(dongle.block)]) + dongle.block)
    dongle.outbox = bytearray(stale) + dongle.outbox
    outcome = session.poll_aggregate()
    assert outcome.ok
    assert outcome.unsolicited_frames == 1


# --- the inverter that refuses ------------------------------------------------


def test_the_downstream_probe_records_the_refusal_both_pilots_return() -> None:
    session, _dongle = connected()
    session.handshake()
    probe = session.probe_downstream_unit(1)
    assert not probe.answered
    assert probe.exception_code == 0x04
    assert probe.exception_name == "slave_device_failure"


def test_a_refusing_inverter_does_not_stop_the_aggregate_data_that_works() -> None:
    """The acceptance criterion, at session level."""
    session, _dongle = connected()
    session.handshake()
    assert not session.probe_downstream_unit(1).answered
    for _ in range(3):
        assert session.poll_aggregate().ok


# --- keep-alive, teardown, reconnect ------------------------------------------


def test_the_keepalive_read_produces_real_traffic_when_a_cycle_is_skipped() -> None:
    session, dongle = connected()
    session.handshake()
    before = len(dongle.sent)
    assert session.keepalive_read()
    assert len(dongle.sent) > before


def test_the_software_register_decodes_to_a_version_string() -> None:
    session, _dongle = connected()
    session.handshake()
    assert session.read_dongle_software() == "V100R001C00SPC124"


def test_the_huawei_device_identification_sequence_returns_the_serial() -> None:
    session, dongle = connected()
    session.handshake()
    assert session.read_device_identification() == {0x87: "HV2340123456"}
    assert dongle.sent[-1].endswith(bytes.fromhex("2B0E0387"))


def test_a_peer_that_hangs_up_mid_poll_raises_session_closed() -> None:
    session, _dongle = connected(reset_after_requests=1)
    session.handshake()
    assert session.poll_aggregate().ok
    with pytest.raises(SessionClosed):
        session.poll_aggregate()


def test_a_peer_that_closes_cleanly_is_reported_as_closed_not_as_an_error() -> None:
    class ClosingDongle(FakeDongle):
        def recv(self, size: int) -> bytes:
            return b""

    session = DongleSession(transport=ClosingDongle(banner=b""))
    with pytest.raises(SessionClosed):
        session.handshake()


def test_a_reconnecting_dongle_starts_a_clean_conversation() -> None:
    """A reconnect is a new session, not a resumed one.

    The transaction counter and the frame buffer both start over, which is
    what stops a half-frame from the dead connection being parsed against the
    new one.
    """
    first, _ = connected(reset_after_requests=0)
    first.handshake()
    with pytest.raises(SessionClosed):
        first.poll_aggregate()
    second, dongle = connected()
    assert second.handshake().serial == "HV2340123456"
    assert second.poll_aggregate().ok
    assert int.from_bytes(dongle.sent[0][0:2], "big") == 1


def test_closing_a_session_closes_its_transport_once() -> None:
    session, dongle = connected()
    session.close()
    session.close()
    assert dongle.closed


def test_an_unterminated_banner_is_accepted_only_after_the_peer_goes_quiet() -> None:
    """What the settle window buys: correctness for both kinds of dongle.

    The pilot hardware never terminates its banner, so waiting for a
    terminator forever is not an option -- but accepting a half-arrived banner
    would attribute a session to the wrong serial, or to no serial at all.
    Silence is the signal that distinguishes them.
    """
    session, dongle = connected(chunk_size=8)
    advertisement = session.handshake()
    assert advertisement.serial == "HV2340123456"
    # Every field made it, not just the ones in the first segment.
    assert advertisement.protocol_version == "1.3"
    assert dongle.timeout is not None


def test_a_device_without_the_aggregate_block_says_so_instead_of_failing_vaguely() -> None:
    """What a SmartLogger is expected to look like.

    0x02 means the register does not exist here, which is a different claim
    from 0x04 (a downstream device declining to answer). Retrying the second
    is sensible; retrying the first never changes anything.
    """
    session, dongle = connected(refuse_units={100: 0x02})
    session.handshake()
    outcome = session.poll_aggregate()

    assert outcome.error_code == "unknown_register_map"
    assert outcome.exception_code == 0x02
    assert outcome.exception_name == "illegal_data_address"


def test_an_unknown_register_map_does_not_trigger_the_register_pair_fallback() -> None:
    """If the block does not exist, its registers one at a time do not either."""
    session, dongle = connected(refuse_units={100: 0x02})
    session.handshake()
    before = len(dongle.sent)
    session.poll_aggregate()

    assert len(dongle.sent) - before == 1, "asked once, not once per signal"
    assert session.block_read_supported, "the fallback was not entered"
