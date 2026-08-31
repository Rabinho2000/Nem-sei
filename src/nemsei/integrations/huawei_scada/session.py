"""The conversation with one dialled-in dongle: handshake, poll, keep-alive.

No sockets here and no database here. The transport is injected, so the whole
protocol conversation -- including a mid-poll disconnect and the reconnect that
follows -- is exercised in tests against a scripted fake rather than against a
real logger nobody can schedule.

The shape of the conversation, in the order it actually happens:

1. The dongle dials in and immediately announces itself with `ADV(J...`.
2. We read that banner, which tells us the serial (who this is) and the unit
   id it answers aggregate reads on (100 on both pilots).
3. From then on we are the Modbus client: one read of the aggregate block per
   poll interval, validated against the request that asked for it.
4. Anything the downstream inverter refuses -- the `0x83`/`0x04` both pilots
   return for `unit=1` -- is recorded and the session carries on. A probe that
   fails must never cost us the aggregate data that works.

The poll *is* the keep-alive. A NAT mapping in front of the customer's router
expires on silence, so `poll_interval_seconds` is required to be shorter than
`idle_timeout_seconds` (enforced in `config.py`). `keepalive_read` exists for
the one case where a poll cycle is deliberately skipped -- a backoff after
repeated protocol errors -- so that backing off does not also mean going
silent and losing the connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Protocol

from nemsei.integrations.huawei_scada import protocol
from nemsei.shared.clock import utc_now

MAX_HANDSHAKE_BYTES = 8192
MAX_UNSOLICITED_FRAMES = 32
RECV_CHUNK = 4096


class SessionTransport(Protocol):
    """The subset of a socket this module uses. A real socket satisfies it."""

    def recv(self, size: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def settimeout(self, seconds: float | None) -> None: ...

    def close(self) -> None: ...


class SessionClosed(Exception):
    """The peer went away. Ordinary: dongles reconnect all the time."""


class SessionProtocolError(Exception):
    """The peer is talking, but not in a way this protocol can represent."""


@dataclass
class PollOutcome:
    """One poll cycle's result, successful or not, always with its evidence."""

    observed_at: datetime
    reading: protocol.AggregatedReading | None = None
    error_code: str | None = None
    safe_detail: str | None = None
    exception_name: str | None = None
    exception_code: int | None = None
    exception_unit_id: int | None = None
    unsolicited_frames: int = 0

    @property
    def ok(self) -> bool:
        return self.reading is not None


@dataclass
class DownstreamProbe:
    """What `unit=1` answered. Expected to fail; recorded, never fatal."""

    unit_id: int
    answered: bool
    exception_name: str | None = None
    exception_code: int | None = None
    safe_detail: str | None = None


@dataclass
class DongleSession:
    """One accepted connection, driven request by request.

    `block_read` reads all five signals in a single 20-register request, which
    is what a healthy dongle answers. `read_signals_individually` is the
    fallback for a firmware that refuses the wide read: five two-register
    reads, same decoding, more round trips. The fallback is entered
    automatically once and remembered, rather than retried on every poll.
    """

    transport: SessionTransport
    poll_interval_seconds: float = 30.0
    read_timeout_seconds: float = 15.0
    handshake_timeout_seconds: float = 60.0
    # How long the banner has to stay quiet before an unterminated one is
    # accepted as finished. The pilot hardware sends no terminator, so without
    # this the handshake would burn the whole timeout on every connection.
    banner_settle_seconds: float = 3.0
    clock: Callable[[], datetime] = utc_now
    advertisement: protocol.DongleAdvertisement | None = None
    unit_id: int = 100
    block_read_supported: bool = True
    _buffer: protocol.FrameBuffer = field(default_factory=protocol.FrameBuffer, repr=False)
    _pending_bytes: bytes = b""
    _ready: list[protocol.ModbusFrame] = field(default_factory=list, repr=False)
    _transaction_id: int = 0
    _closed: bool = False

    # --- handshake ----------------------------------------------------------

    def handshake(self) -> protocol.DongleAdvertisement:
        """Read the opening banner, or refuse the session.

        A peer that connects and says nothing is not a dongle we can identify,
        and identifying it is the only thing that decides which asset its data
        belongs to. There is no fallback identity here, by design.
        """
        deadline = self._deadline(self.handshake_timeout_seconds)
        buffer = b""
        while True:
            if len(buffer) > MAX_HANDSHAKE_BYTES:
                raise SessionProtocolError("Dongle sent no advertisement within the handshake budget.")
            advertisement, tail = protocol.extract_advertisement(buffer)
            if advertisement is not None:
                return self._adopt(advertisement, tail)
            remaining = self._remaining(deadline)
            if remaining <= 0:
                # Out of time. If an unterminated banner is sitting in the
                # buffer with a serial in it, that is the pilot hardware's
                # normal behaviour, not a failure.
                advertisement, tail = protocol.extract_advertisement(buffer, allow_unterminated=True)
                if advertisement is not None:
                    return self._adopt(advertisement, tail)
                raise TimeoutError("Timed out waiting for the dongle advertisement.")
            try:
                buffer += self._recv(min(remaining, self.banner_settle_seconds))
            except TimeoutError:
                # The peer stopped talking. Whatever it said is all it is
                # going to say, so an unterminated banner is now complete.
                advertisement, tail = protocol.extract_advertisement(buffer, allow_unterminated=True)
                if advertisement is not None:
                    return self._adopt(advertisement, tail)
                if self._remaining(deadline) <= 0:
                    raise

    def _adopt(self, advertisement: protocol.DongleAdvertisement, tail: bytes) -> protocol.DongleAdvertisement:
        self.advertisement = advertisement
        self.unit_id = advertisement.aggregate_unit_id
        self._pending_bytes = tail
        return advertisement

    # --- polling ------------------------------------------------------------

    def poll_aggregate(self) -> PollOutcome:
        """Read the five aggregate signals once. Never raises for a bad answer.

        A poll that fails returns a `PollOutcome` describing how, because the
        caller has to persist that failure as session evidence and then keep
        going. Only a dead transport propagates, since that ends the session
        rather than the poll.
        """
        observed_at = self.clock()
        try:
            if self.block_read_supported:
                try:
                    return self._poll_block(observed_at)
                except protocol.ModbusExceptionResponse as exception:
                    if exception.is_unknown_register:
                        # The block does not exist on this device, so neither
                        # do its registers one at a time. Falling back would
                        # just ask the same wrong question five more times.
                        raise
                    # A refusal of the *wide* read is not a refusal of the
                    # data: some firmware only answers register-pair reads.
                    # Fall back once, remember it, and try immediately.
                    self.block_read_supported = False
                    outcome = self._poll_individually(observed_at)
                    outcome.safe_detail = (
                        f"block read refused ({exception.name}); fell back to individual register reads"
                    )
                    return outcome
            return self._poll_individually(observed_at)
        except protocol.ModbusExceptionResponse as exception:
            return PollOutcome(
                observed_at=observed_at,
                error_code="unknown_register_map" if exception.is_unknown_register else "modbus_exception",
                exception_name=exception.name,
                exception_code=exception.exception_code,
                exception_unit_id=exception.unit_id,
                safe_detail=str(exception),
            )
        except protocol.HuaweiProtocolError as error:
            return PollOutcome(observed_at=observed_at, error_code="protocol_error", safe_detail=str(error))
        except TimeoutError:
            return PollOutcome(observed_at=observed_at, error_code="timeout", safe_detail="No answer within the read timeout.")

    def _poll_block(self, observed_at: datetime) -> PollOutcome:
        transaction_id = self._next_transaction_id()
        self._send(
            protocol.build_read_holding_registers(
                transaction_id=transaction_id,
                unit_id=self.unit_id,
                address=protocol.AGGREGATE_BLOCK_START,
                quantity=protocol.AGGREGATE_BLOCK_QUANTITY,
            )
        )
        frame, unsolicited = self._await_frame(transaction_id)
        body = protocol.read_register_payload(
            frame,
            expected_transaction_id=transaction_id,
            expected_unit_id=self.unit_id,
            expected_quantity=protocol.AGGREGATE_BLOCK_QUANTITY,
        )
        return PollOutcome(
            observed_at=observed_at,
            reading=protocol.parse_aggregated_block(body),
            unsolicited_frames=unsolicited,
        )

    def _poll_individually(self, observed_at: datetime) -> PollOutcome:
        raw: dict[str, int | None] = {}
        values: dict[str, Decimal | None] = {}
        unsolicited = 0
        for name, address, _signed in protocol.AGGREGATE_SIGNALS:
            transaction_id = self._next_transaction_id()
            self._send(
                protocol.build_read_holding_registers(
                    transaction_id=transaction_id, unit_id=self.unit_id, address=address, quantity=2
                )
            )
            frame, skipped = self._await_frame(transaction_id)
            unsolicited += skipped
            body = protocol.read_register_payload(
                frame, expected_transaction_id=transaction_id, expected_unit_id=self.unit_id, expected_quantity=2
            )
            raw_value, scaled_value = protocol.parse_single_signal(name, body)
            raw[str(address)] = raw_value
            values[name] = scaled_value
        reading = protocol.AggregatedReading(
            pv_input_power_kw=values["pv_input_power"],
            load_power_kw=values["load_power"],
            grid_power_kw=values["grid_power"],
            battery_power_kw=values["battery_power"],
            total_active_power_kw=values["total_active_power"],
            raw_registers=raw,
        )
        return PollOutcome(observed_at=observed_at, reading=reading, unsolicited_frames=unsolicited)

    # --- optional probes ----------------------------------------------------

    def probe_downstream_unit(self, unit_id: int = 1) -> DownstreamProbe:
        """Ask the inverter directly. Both pilots answer 0x83/0x04.

        Kept because "the inverter still refuses" is worth knowing and worth
        re-checking cheaply, and because the day one answers, the evidence
        that it now does will already be in the session record. It is never
        required: `listener.py` treats any result as informational.
        """
        transaction_id = self._next_transaction_id()
        try:
            self._send(
                protocol.build_read_holding_registers(
                    transaction_id=transaction_id, unit_id=unit_id, address=protocol.REGISTER_PV_INPUT_POWER, quantity=2
                )
            )
            frame, _unsolicited = self._await_frame(transaction_id)
            protocol.read_register_payload(
                frame, expected_transaction_id=transaction_id, expected_unit_id=unit_id, expected_quantity=2
            )
        except protocol.ModbusExceptionResponse as exception:
            return DownstreamProbe(
                unit_id=unit_id,
                answered=False,
                exception_name=exception.name,
                exception_code=exception.exception_code,
                safe_detail=str(exception),
            )
        except (protocol.HuaweiProtocolError, TimeoutError) as error:
            return DownstreamProbe(unit_id=unit_id, answered=False, safe_detail=str(error))
        return DownstreamProbe(unit_id=unit_id, answered=True)

    def read_dongle_software(self) -> str | None:
        """Register 30050. Informational; a refusal is not a session failure."""
        transaction_id = self._next_transaction_id()
        try:
            self._send(
                protocol.build_read_holding_registers(
                    transaction_id=transaction_id,
                    unit_id=self.unit_id,
                    address=protocol.REGISTER_DONGLE_SOFTWARE,
                    quantity=protocol.DONGLE_SOFTWARE_REGISTER_COUNT,
                )
            )
            frame, _unsolicited = self._await_frame(transaction_id)
            body = protocol.read_register_payload(
                frame,
                expected_transaction_id=transaction_id,
                expected_unit_id=self.unit_id,
                expected_quantity=protocol.DONGLE_SOFTWARE_REGISTER_COUNT,
            )
        except (protocol.ModbusExceptionResponse, protocol.HuaweiProtocolError, TimeoutError):
            return None
        return protocol.decode_ascii_registers(body) or None

    def read_device_identification(self) -> dict[int, str]:
        """The `2B 0E 03 87` probe. Empty dict when the dongle declines."""
        transaction_id = self._next_transaction_id()
        try:
            self._send(protocol.build_read_device_identification(transaction_id=transaction_id, unit_id=self.unit_id))
            frame, _unsolicited = self._await_frame(transaction_id)
            return protocol.read_device_identification_payload(
                frame, expected_transaction_id=transaction_id, expected_unit_id=self.unit_id
            )
        except (protocol.ModbusExceptionResponse, protocol.HuaweiProtocolError, TimeoutError):
            return {}

    def keepalive_read(self) -> bool:
        """Cheap traffic for a cycle that is deliberately not sampling.

        Backing off after repeated errors must not mean going silent: the NAT
        mapping the dongle dialled through expires on silence, and losing it
        costs a full reconnect for a problem that was only transient.
        """
        return self.read_dongle_software() is not None

    # --- plumbing -----------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.transport.close()
        except OSError:  # pragma: no cover - closing a dead socket is not news
            pass

    def _next_transaction_id(self) -> int:
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        return self._transaction_id

    def _send(self, frame: bytes) -> None:
        try:
            self.transport.sendall(frame)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise SessionClosed("Dongle closed the connection while a request was being sent.") from exc
        except TimeoutError:
            raise
        except OSError as exc:
            raise SessionClosed(f"Transport failed while sending: {exc}") from exc

    def _recv(self, timeout: float) -> bytes:
        self.transport.settimeout(timeout)
        try:
            chunk = self.transport.recv(RECV_CHUNK)
        except TimeoutError:
            raise
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise SessionClosed("Dongle reset the connection.") from exc
        except OSError as exc:
            raise SessionClosed(f"Transport failed while reading: {exc}") from exc
        if not chunk:
            raise SessionClosed("Dongle closed the connection.")
        return chunk

    def _await_frame(self, transaction_id: int) -> tuple[protocol.ModbusFrame, int]:
        """Wait for the answer to *this* request, discarding foreign frames.

        A frame carrying someone else's transaction id is a leftover from a
        previous, timed-out request. Accepting it would attribute an old
        measurement to the present instant, so it is counted and dropped --
        and if only leftovers arrive, this times out rather than settling for
        one.
        """
        deadline = self._deadline(self.read_timeout_seconds)
        unsolicited = 0
        frames, self._ready = self._ready, []
        if self._pending_bytes:
            pending, self._pending_bytes = self._pending_bytes, b""
            frames = frames + self._buffer.feed(pending)
        while True:
            for index, frame in enumerate(frames):
                if frame.transaction_id == transaction_id:
                    # Anything that arrived grouped behind the answer is kept,
                    # not dropped: the next request will see it, recognise it
                    # as foreign and count it. Discarding here would hide a
                    # dongle that talks out of turn.
                    self._ready = frames[index + 1 :]
                    return frame, unsolicited
                unsolicited += 1
                if unsolicited > MAX_UNSOLICITED_FRAMES:
                    raise SessionProtocolError("Dongle is answering requests that were never made.")
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for a Modbus response.")
            frames = self._buffer.feed(self._recv(remaining))

    def _deadline(self, seconds: float) -> float:
        return self.clock().timestamp() + seconds

    def _remaining(self, deadline: float) -> float:
        return deadline - self.clock().timestamp()

    def describe(self) -> dict[str, Any]:
        """Provenance for the session row, from the banner the dongle sent."""
        if self.advertisement is None:
            return {}
        return {
            "dongle_model": self.advertisement.model,
            "dongle_software_version": self.advertisement.software_version,
            "protocol_version": self.advertisement.protocol_version,
            "aggregate_unit_id": self.advertisement.aggregate_unit_id,
            "advertisement_fields": dict(self.advertisement.fields),
        }
