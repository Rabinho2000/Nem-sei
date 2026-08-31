"""Wire decoding for the Huawei SDongle SCADA/NMS session. No I/O lives here.

The logger dials *out* to us, so the roles are inverted relative to ordinary
Modbus TCP: the dongle owns the socket, and this server is the Modbus client
speaking over a connection it did not open. Everything below is therefore a
pure function over bytes -- `session.py` owns the conversation, `listener.py`
owns the socket, and this module can be tested against captured frames alone.

Three properties of the observed traffic drive the shape of this module:

* **The stream is a stream.** One `recv()` can carry half a frame, or three
  frames, or a frame plus the tail of the banner. `FrameBuffer` is the only
  correct place to deal with that, and it deals with it once.
* **The dongle answers for itself, not for the inverter.** On both piloted
  installations a read against `unit=1` came back `function=0x83`,
  `exception=0x04` ("slave device failure"). That is a decoded, named answer
  here -- `ModbusExceptionResponse` -- not a parse error, because a session
  that treats it as a fault stops collecting the aggregate data that *does*
  work.
* **A register can say "I don't know".** Huawei writes `0xFFFFFFFF` (U32) and
  `0x7FFFFFFF` (I32) for an unavailable measurement. Decoding those to
  4 294 967.295 kW would be a fabricated reading, so they decode to `None`.

Nothing in this module writes, and nothing in it can: there is no encoder for
function 0x06 or 0x10 (write single/multiple registers). The only frames it
knows how to build are reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator

# MBAP: transaction (2) | protocol (2) | length (2) | unit (1)
MBAP_HEADER_LENGTH = 7
MBAP_LENGTH_OFFSET = 4
MODBUS_PROTOCOL_ID = 0
# A Modbus TCP ADU is capped at 260 bytes, so `length` (everything after the
# length field itself) can never exceed 254. A larger value means the stream
# is desynchronised, and continuing to buffer would let a corrupt peer make
# this process allocate without bound.
MAX_MBAP_LENGTH = 254

FUNCTION_READ_HOLDING_REGISTERS = 0x03
FUNCTION_READ_DEVICE_IDENTIFICATION = 0x2B
MEI_TYPE_DEVICE_IDENTIFICATION = 0x0E
# The Huawei device-identification probe seen on the wire: 2B 0E 03 87.
DEVICE_ID_CODE_EXTENDED = 0x03
DEVICE_ID_OBJECT_HUAWEI = 0x87
EXCEPTION_FLAG = 0x80

# Modbus exception names, so an error is logged as a meaning rather than a
# number. 0x04 is the one both pilot installations return for `unit=1`.
EXCEPTION_NAMES = {
    0x01: "illegal_function",
    0x02: "illegal_data_address",
    0x03: "illegal_data_value",
    0x04: "slave_device_failure",
    0x05: "acknowledge",
    0x06: "slave_device_busy",
    0x08: "memory_parity_error",
    0x0A: "gateway_path_unavailable",
    0x0B: "gateway_target_no_response",
}
SLAVE_DEVICE_FAILURE = 0x04
# "This register does not exist here." Distinct from 0x04 in a way that
# matters: 0x04 is a downstream device refusing to answer (retry later), while
# 0x02 means the map itself is wrong for this device -- a SmartLogger does not
# implement the SDongle's aggregate block, and retrying will never change that.
ILLEGAL_DATA_ADDRESS = 0x02

# The aggregate block the dongle answers for on its own unit. Addresses are
# the observed ones; each value is two registers wide.
REGISTER_PV_INPUT_POWER = 37498       # 0x927A, U32
REGISTER_LOAD_POWER = 37500           # 0x927C, U32
REGISTER_GRID_POWER = 37502           # 0x927E, I32
REGISTER_BATTERY_POWER = 37504        # 0x9280, I32
REGISTER_TOTAL_ACTIVE_POWER = 37516   # 0x928C, U32
REGISTER_DONGLE_SOFTWARE = 30050      # 0x7562, ASCII
DONGLE_SOFTWARE_REGISTER_COUNT = 15

AGGREGATE_BLOCK_START = REGISTER_PV_INPUT_POWER
# 37498..37517 inclusive: everything through the second register of 37516.
AGGREGATE_BLOCK_QUANTITY = (REGISTER_TOTAL_ACTIVE_POWER + 2) - REGISTER_PV_INPUT_POWER

# name -> (address, signed). The scale is 1000 for every one of them, which is
# asserted rather than assumed: `POWER_SCALE` is a single constant precisely so
# a future register with a different gain cannot be added without noticing.
POWER_SCALE = 1000
AGGREGATE_SIGNALS: tuple[tuple[str, int, bool], ...] = (
    ("pv_input_power", REGISTER_PV_INPUT_POWER, False),
    ("load_power", REGISTER_LOAD_POWER, False),
    ("grid_power", REGISTER_GRID_POWER, True),
    ("battery_power", REGISTER_BATTERY_POWER, True),
    ("total_active_power", REGISTER_TOTAL_ACTIVE_POWER, False),
)

# Sentinels. Huawei uses the full-scale value to mean "not available"; 0x80000000
# is the other end of the same idea for a signed register.
U16_SENTINEL = 0xFFFF
U32_SENTINEL = 0xFFFFFFFF
I32_SENTINELS = (0x7FFFFFFF, -0x80000000)

# The announcement is NOT an ASCII line. Captured from the real SDongleA-05:
#
#   00 00 00 00 00 5b 00 41 44 00 56 01 05 2c 00 00 06 00 01 01 00 12 4a 31 3d ...
#                        A  D  \0 V                                   J  1  =
#
# "ADV(J1=..." was a human transcription of a proprietary binary header whose
# printable bytes happen to read that way -- there is no literal "ADV(" on the
# wire, and an "A", "D" and "V" that are not even contiguous. Matching that
# transcription is why the first real dongle to dial in was never identified.
#
# So the banner is found by the only part of it that is genuinely text: the
# run of `N=value;` fields. That works whatever wrapper Huawei puts around it.
_FIELD_RUN = re.compile(rb"(?:[0-9]{1,2}=[ -~]*?;){2,}[0-9]{1,2}=[ -~]*")
# The serial is only trustworthy once its own separator has arrived; without
# this a banner split mid-serial would identify the dongle by half a serial.
_SERIAL_COMPLETE = re.compile(rb"(?:^|;)4=[ -~]*?;")


class HuaweiProtocolError(ValueError):
    """The bytes on the wire are not a frame this protocol can represent."""


@dataclass(frozen=True)
class ModbusExceptionResponse(Exception):
    """A well-formed negative answer, e.g. `unit=1` replying 0x83/0x04.

    Deliberately an exception *and* a value: `session.py` catches it to keep
    polling, and `ingestion.py` stores its name as evidence. A downstream
    inverter that refuses to answer is a fact about the installation, not a
    failure of this session.
    """

    unit_id: int
    function: int
    exception_code: int

    @property
    def name(self) -> str:
        return EXCEPTION_NAMES.get(self.exception_code, f"unknown_{self.exception_code:#04x}")

    @property
    def is_downstream_failure(self) -> bool:
        """True for the 0x04 both pilots return when asked about `unit=1`."""
        return self.exception_code == SLAVE_DEVICE_FAILURE

    @property
    def is_unknown_register(self) -> bool:
        """True when the device does not implement the register that was asked for.

        Worth its own name because the right response is the opposite of a
        transient failure: stop asking. The SDongle aggregate block is not a
        universal Huawei map, and a device that answers 0x02 to it is telling
        us so in the clearest terms the protocol has.
        """
        return self.exception_code == ILLEGAL_DATA_ADDRESS

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"unit {self.unit_id} answered {self.function:#04x}/{self.exception_code:#04x} ({self.name})"


@dataclass(frozen=True)
class ModbusFrame:
    transaction_id: int
    unit_id: int
    function: int
    payload: bytes

    @property
    def is_exception(self) -> bool:
        return bool(self.function & EXCEPTION_FLAG)


@dataclass(frozen=True)
class DongleAdvertisement:
    """The dongle's opening line, parsed into what it actually claims.

    Observed form:
    `ADV(J1=SDongle...;2=V...;3=P...;4=<serial>;5=100;6=1.3`

    Field 5 is the unit id the dongle answers aggregate reads on -- 100 on both
    pilots. It is read from the banner rather than hardcoded, and only falls
    back to 100 when the banner omits it, so a dongle that announces a
    different unit is polled where it said to poll.
    """

    serial: str
    model: str | None
    software_version: str | None
    product: str | None
    aggregate_unit_id: int
    protocol_version: str | None
    fields: dict[str, str]
    raw: str


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def decode_u16(data: bytes, offset: int) -> int | None:
    value = _u16(data, offset)
    return None if value == U16_SENTINEL else value


def decode_u32(data: bytes, offset: int) -> int | None:
    """Two big-endian registers, high word first, sentinel-aware."""
    if offset + 4 > len(data):
        raise HuaweiProtocolError("Register block is too short for a U32 value.")
    value = int.from_bytes(data[offset : offset + 4], "big", signed=False)
    return None if value == U32_SENTINEL else value


def decode_i32(data: bytes, offset: int) -> int | None:
    if offset + 4 > len(data):
        raise HuaweiProtocolError("Register block is too short for an I32 value.")
    value = int.from_bytes(data[offset : offset + 4], "big", signed=True)
    return None if value in I32_SENTINELS else value


def scaled(raw: int | None, *, scale: int = POWER_SCALE) -> Decimal | None:
    """Apply the register gain exactly. `Decimal`, never float.

    A power sample is integrated into billable energy later, so a binary
    rounding error introduced here would survive all the way into a customer
    report. `Decimal(raw) / 1000` is exact for every integer the wire can
    carry.
    """
    if raw is None:
        return None
    if scale <= 0:
        raise HuaweiProtocolError("Register scale must be positive.")
    return Decimal(raw) / Decimal(scale)


def decode_ascii_registers(payload: bytes) -> str:
    """A Huawei string register block: ASCII, NUL-padded, sometimes trailing junk."""
    text = payload.split(b"\x00", 1)[0]
    return text.decode("ascii", errors="ignore").strip()


class FrameBuffer:
    """Reassembles MBAP frames out of an arbitrarily chopped byte stream.

    Feed it whatever `recv()` returned; it yields every *complete* frame and
    keeps the remainder for next time. A frame split across three reads and
    three frames delivered in one read are the same case to it, which is the
    only way this can be right: TCP guarantees neither.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def __len__(self) -> int:
        return len(self._buffer)

    @property
    def pending(self) -> bytes:
        return bytes(self._buffer)

    def feed(self, chunk: bytes) -> list[ModbusFrame]:
        self._buffer.extend(chunk)
        return list(self._drain())

    def _drain(self) -> Iterator[ModbusFrame]:
        while True:
            if len(self._buffer) < MBAP_HEADER_LENGTH:
                return
            protocol_id = _u16(self._buffer, 2)
            if protocol_id != MODBUS_PROTOCOL_ID:
                raise HuaweiProtocolError(f"MBAP protocol id {protocol_id} is not Modbus.")
            length = _u16(self._buffer, MBAP_LENGTH_OFFSET)
            if length < 2 or length > MAX_MBAP_LENGTH:
                raise HuaweiProtocolError(f"MBAP length {length} is out of range.")
            total = MBAP_LENGTH_OFFSET + 2 + length
            if len(self._buffer) < total:
                return
            frame = bytes(self._buffer[:total])
            del self._buffer[:total]
            yield ModbusFrame(
                transaction_id=_u16(frame, 0),
                unit_id=frame[6],
                function=frame[7],
                payload=frame[8:],
            )


def build_read_holding_registers(*, transaction_id: int, unit_id: int, address: int, quantity: int) -> bytes:
    """Function 0x03. The only read this integration ever needs for values."""
    if not 0 <= transaction_id <= 0xFFFF:
        raise HuaweiProtocolError("Transaction id must fit in two bytes.")
    if not 0 <= unit_id <= 0xFF:
        raise HuaweiProtocolError("Unit id must fit in one byte.")
    if not 0 <= address <= 0xFFFF:
        raise HuaweiProtocolError("Register address must fit in two bytes.")
    if not 1 <= quantity <= 125:
        raise HuaweiProtocolError("Modbus reads are limited to 125 registers.")
    pdu = bytes([FUNCTION_READ_HOLDING_REGISTERS]) + address.to_bytes(2, "big") + quantity.to_bytes(2, "big")
    return _wrap(transaction_id, unit_id, pdu)


def build_read_device_identification(
    *,
    transaction_id: int,
    unit_id: int,
    read_device_id: int = DEVICE_ID_CODE_EXTENDED,
    object_id: int = DEVICE_ID_OBJECT_HUAWEI,
) -> bytes:
    """The `2B 0E 03 87` sequence, built rather than replayed as a literal."""
    pdu = bytes([FUNCTION_READ_DEVICE_IDENTIFICATION, MEI_TYPE_DEVICE_IDENTIFICATION, read_device_id, object_id])
    return _wrap(transaction_id, unit_id, pdu)


def _wrap(transaction_id: int, unit_id: int, pdu: bytes) -> bytes:
    length = len(pdu) + 1
    return (
        transaction_id.to_bytes(2, "big")
        + MODBUS_PROTOCOL_ID.to_bytes(2, "big")
        + length.to_bytes(2, "big")
        + bytes([unit_id])
        + pdu
    )


def read_register_payload(
    frame: ModbusFrame,
    *,
    expected_transaction_id: int,
    expected_unit_id: int,
    expected_quantity: int,
) -> bytes:
    """Validate a 0x03 answer against what was actually asked, then unwrap it.

    All three checks matter, and none is theoretical on a shared TCP stream:
    a stale answer from a previous poll carries the wrong transaction id, a
    reply about a different unit is about a different device, and a byte count
    that disagrees with the request means the block is not the block that was
    asked for.
    """
    _require_match(frame, expected_transaction_id=expected_transaction_id, expected_unit_id=expected_unit_id)
    if frame.function != FUNCTION_READ_HOLDING_REGISTERS:
        raise HuaweiProtocolError(f"Expected function 0x03, got {frame.function:#04x}.")
    if not frame.payload:
        raise HuaweiProtocolError("Register response carries no byte count.")
    byte_count = frame.payload[0]
    body = frame.payload[1:]
    if byte_count != expected_quantity * 2:
        raise HuaweiProtocolError(f"Register response declares {byte_count} bytes, expected {expected_quantity * 2}.")
    if len(body) != byte_count:
        raise HuaweiProtocolError("Register response body does not match its declared byte count.")
    return body


def read_device_identification_payload(
    frame: ModbusFrame, *, expected_transaction_id: int, expected_unit_id: int
) -> dict[int, str]:
    """Decode a 0x2B/0x0E answer into {object id: value}."""
    _require_match(frame, expected_transaction_id=expected_transaction_id, expected_unit_id=expected_unit_id)
    if frame.function != FUNCTION_READ_DEVICE_IDENTIFICATION:
        raise HuaweiProtocolError(f"Expected function 0x2B, got {frame.function:#04x}.")
    body = frame.payload
    if len(body) < 6 or body[0] != MEI_TYPE_DEVICE_IDENTIFICATION:
        raise HuaweiProtocolError("Device identification response is malformed.")
    count = body[5]
    cursor = 6
    objects: dict[int, str] = {}
    for _ in range(count):
        if cursor + 2 > len(body):
            raise HuaweiProtocolError("Device identification response is truncated.")
        object_id, length = body[cursor], body[cursor + 1]
        cursor += 2
        if cursor + length > len(body):
            raise HuaweiProtocolError("Device identification object is truncated.")
        objects[object_id] = body[cursor : cursor + length].decode("ascii", errors="ignore").strip()
        cursor += length
    return objects


def _require_match(frame: ModbusFrame, *, expected_transaction_id: int, expected_unit_id: int) -> None:
    if frame.transaction_id != expected_transaction_id:
        raise HuaweiProtocolError(
            f"Transaction id {frame.transaction_id} does not answer request {expected_transaction_id}."
        )
    if frame.unit_id != expected_unit_id:
        raise HuaweiProtocolError(f"Response is for unit {frame.unit_id}, not {expected_unit_id}.")
    if frame.is_exception:
        code = frame.payload[0] if frame.payload else 0
        raise ModbusExceptionResponse(unit_id=frame.unit_id, function=frame.function, exception_code=code)


@dataclass(frozen=True)
class AggregatedReading:
    """The five aggregate signals, in kW, plus the raw registers behind them.

    `raw_registers` is kept because the scaled value is an interpretation and
    the register is the evidence. If the gain for one of these addresses ever
    turns out to be something other than 1000, the stored samples can be
    re-derived instead of re-collected.
    """

    pv_input_power_kw: Decimal | None
    load_power_kw: Decimal | None
    grid_power_kw: Decimal | None
    battery_power_kw: Decimal | None
    total_active_power_kw: Decimal | None
    raw_registers: dict[str, int | None]

    @property
    def values(self) -> dict[str, Decimal | None]:
        return {
            "pv_input_power_kw": self.pv_input_power_kw,
            "load_power_kw": self.load_power_kw,
            "grid_power_kw": self.grid_power_kw,
            "battery_power_kw": self.battery_power_kw,
            "total_active_power_kw": self.total_active_power_kw,
        }

    @property
    def signal_count(self) -> int:
        return sum(1 for value in self.values.values() if value is not None)


def parse_aggregated_block(body: bytes, *, base_address: int = AGGREGATE_BLOCK_START) -> AggregatedReading:
    """Pull the five signals out of one contiguous register block."""
    raw: dict[str, int | None] = {}
    scaled_values: dict[str, Decimal | None] = {}
    for name, address, signed in AGGREGATE_SIGNALS:
        offset = (address - base_address) * 2
        if offset < 0 or offset + 4 > len(body):
            raise HuaweiProtocolError(f"Register block does not cover {name} at {address}.")
        value = decode_i32(body, offset) if signed else decode_u32(body, offset)
        raw[str(address)] = value
        scaled_values[name] = scaled(value)
    return AggregatedReading(
        pv_input_power_kw=scaled_values["pv_input_power"],
        load_power_kw=scaled_values["load_power"],
        grid_power_kw=scaled_values["grid_power"],
        battery_power_kw=scaled_values["battery_power"],
        total_active_power_kw=scaled_values["total_active_power"],
        raw_registers=raw,
    )


def parse_single_signal(name: str, body: bytes) -> tuple[int | None, Decimal | None]:
    """Decode one two-register read, for the fallback path that reads singly."""
    for signal, _address, signed in AGGREGATE_SIGNALS:
        if signal != name:
            continue
        raw = decode_i32(body, 0) if signed else decode_u32(body, 0)
        return raw, scaled(raw)
    raise HuaweiProtocolError(f"Unknown aggregate signal {name!r}.")


def extract_advertisement(buffer: bytes, *, allow_unterminated: bool = False) -> tuple[DongleAdvertisement | None, bytes]:
    """Find the opening `ADV(J...` line, returning it and the unconsumed tail.

    A banner is complete when it is terminated -- `)`, CR, LF or NUL. Without
    that rule a partially-arrived banner parses as a whole one: `ADV(J1=...;2=`
    delivered on its own looks like a valid announcement that happens to be
    missing its serial, and a dongle whose banner crossed a TCP segment
    boundary would be refused every single time it dialled in.

    `allow_unterminated` is the deliberate escape hatch, used by `handshake`
    only after the peer has gone quiet: the traffic captured from the pilot
    hardware carries no terminator at all, so requiring one unconditionally
    would mean never completing a handshake against the real thing. Waiting
    for silence first is what distinguishes "the dongle does not terminate its
    banner" from "the rest of the banner has not arrived yet".
    """
    match = _FIELD_RUN.search(buffer)
    if match is None:
        return None, buffer
    raw_body = match.group(0)
    # Some firmware closes the run with ")"; the captured one does not. Cutting
    # here rather than in the pattern keeps the pattern readable.
    body = raw_body.split(b")", 1)[0].decode("ascii", errors="ignore")
    fields: dict[str, str] = {}
    for part in body.split(";"):
        key, separator, value = part.partition("=")
        if separator and key.strip():
            fields[key.strip()] = value.strip()
    serial = fields.get("4", "").strip()
    # Without a terminator there is no way to know the run has ended, and
    # stopping early is not harmless: field 5 carries the Modbus unit to poll,
    # so a banner accepted at the serial would silently fall back to 100 and
    # a dongle answering on any other unit would never be read. Waiting for
    # the peer to go quiet costs a few seconds once per connection.
    end = match.end()
    # ")" is printable, so it lands *inside* the match; CR, LF and NUL are not,
    # so they land just after it. Both are terminators and both have to be
    # recognised, which is why this reads the match and the next byte.
    terminated = b")" in raw_body or buffer[end : end + 1] in (b"\r", b"\n", b"\x00")
    if not terminated and not allow_unterminated:
        return None, buffer
    if not serial or _SERIAL_COMPLETE.search(raw_body) is None:
        if not allow_unterminated:
            return None, buffer
        if not serial:
            raise HuaweiProtocolError("Dongle advertisement carries no serial number.")
    unit_field = fields.get("5", "").strip()
    try:
        aggregate_unit = int(unit_field) if unit_field else 100
    except ValueError as exc:
        raise HuaweiProtocolError("Dongle advertisement unit id is not numeric.") from exc
    if not 0 <= aggregate_unit <= 0xFF:
        raise HuaweiProtocolError("Dongle advertisement unit id is out of range.")
    advertisement = DongleAdvertisement(
        serial=serial,
        model=fields.get("1") or None,
        software_version=fields.get("2") or None,
        product=fields.get("3") or None,
        aggregate_unit_id=aggregate_unit,
        protocol_version=fields.get("6") or None,
        fields=fields,
        raw=body,
    )
    return advertisement, buffer[match.end() :]
