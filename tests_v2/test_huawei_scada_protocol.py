"""Frame-level tests against the protocol as it was actually observed.

The fixtures here are built from the real pilot traffic described in
`docs/v2/HUAWEI_SCADA.md`: the dongle's opening `ADV(J...` line, the aggregate
register block on `unit=100`, and the `0x83`/`0x04` refusal both installations
returned for `unit=1`.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from nemsei.integrations.huawei_scada import protocol as p


# Captured byte for byte off the real SDongleA-05 at Centro Julia Moreira,
# 2026-08-26 18:54 (dongle TA2430080460). This is the whole first packet: a
# proprietary binary header, then the ASCII field run. There is no literal
# "ADV(" anywhere in it -- the "A", "D" and "V" are not even contiguous.
CAPTURED_ANNOUNCEMENT = bytes.fromhex(
    "00 00 00 00 00 5b 00 41 44 00 56 01 05 2c 00 00 06 00 01 01 00 12 4a"
    "31 3d 53 44 6f 6e 67 6c 65 41 2d 30 35 3b 32 3d 56 32 30 30 52 30 32"
    "32 43 31 30 53 50 43 33 31 32 3b 33 3d 50 31 2e 31 35 2d 44 35 30 2e"
    "30 3b 34 3d 54 41 32 34 33 30 30 38 30 34 36 30 3b 35 3d 31 30 30 3b"
    "36 3d 31 2e 33".replace(" ", "")
)
# A second, synthetic shape used by the older fixtures: same field run, no
# binary wrapper. Both must parse, because the wrapper is not the contract.
REAL_ADVERTISEMENT = b"ADV(J1=SDongle-WLAN-FE;2=V100R001C00SPC124;3=P1;4=HV2340123456;5=100;6=1.3"


def test_the_captured_dongle_announcement_is_parsed() -> None:
    """The regression that the first live connection exposed.

    The parser used to require a literal `ADV(J` prefix, taken from a human
    transcription of these bytes. Against the real hardware it matched
    nothing, every session timed out waiting for a banner that had already
    arrived, and not one sample was collected.
    """
    advertisement, tail = p_extract(CAPTURED_ANNOUNCEMENT, allow_unterminated=True)
    assert advertisement is not None
    assert advertisement.serial == "TA2430080460"
    assert advertisement.model == "SDongleA-05"
    assert advertisement.software_version == "V200R022C10SPC312"
    assert advertisement.product == "P1.15-D50.0"
    assert advertisement.aggregate_unit_id == 100
    assert advertisement.protocol_version == "1.3"
    assert tail == b""


def test_the_captured_announcement_waits_for_silence_before_it_is_trusted() -> None:
    """It carries no terminator, so the run might still be growing.

    Accepting it the moment the serial closes loses field 5 -- the Modbus unit
    to poll -- which is exactly the fragmented-delivery bug the session test
    caught. The few seconds of silence are what make the rest of the fields
    safe to read.
    """
    assert p_extract(CAPTURED_ANNOUNCEMENT, allow_unterminated=False) == (None, CAPTURED_ANNOUNCEMENT)


def test_the_captured_announcement_arriving_in_fragments_is_not_half_read() -> None:
    """Cut mid-serial, it must report nothing rather than half an identity."""
    cut = CAPTURED_ANNOUNCEMENT.index(b"4=TA2430080460") + len("4=TA24300")
    advertisement, _tail = p_extract(CAPTURED_ANNOUNCEMENT[:cut])
    assert advertisement is None


def p_extract(buffer, **kwargs):
    return p.extract_advertisement(buffer, **kwargs)


def aggregate_block(
    *, pv: int | None = 5432, load: int | None = 3120, grid: int | None = -2312,
    battery: int | None = 0, total: int | None = 5400,
) -> bytes:
    """40 bytes covering 37498..37517, the block one poll reads."""
    body = bytearray(p.AGGREGATE_BLOCK_QUANTITY * 2)
    def place(address: int, value: int | None, *, signed: bool) -> None:
        offset = (address - p.AGGREGATE_BLOCK_START) * 2
        if value is None:
            raw = p.I32_SENTINELS[0] if signed else p.U32_SENTINEL
            body[offset : offset + 4] = raw.to_bytes(4, "big", signed=False)
            return
        body[offset : offset + 4] = value.to_bytes(4, "big", signed=signed)

    place(p.REGISTER_PV_INPUT_POWER, pv, signed=False)
    place(p.REGISTER_LOAD_POWER, load, signed=False)
    place(p.REGISTER_GRID_POWER, grid, signed=True)
    place(p.REGISTER_BATTERY_POWER, battery, signed=True)
    place(p.REGISTER_TOTAL_ACTIVE_POWER, total, signed=False)
    return bytes(body)


def register_response(transaction_id: int, unit_id: int, body: bytes) -> bytes:
    pdu = bytes([p.FUNCTION_READ_HOLDING_REGISTERS, len(body)]) + body
    return (
        transaction_id.to_bytes(2, "big")
        + b"\x00\x00"
        + (len(pdu) + 1).to_bytes(2, "big")
        + bytes([unit_id])
        + pdu
    )


def exception_response(transaction_id: int, unit_id: int, function: int, code: int) -> bytes:
    pdu = bytes([function | p.EXCEPTION_FLAG, code])
    return (
        transaction_id.to_bytes(2, "big")
        + b"\x00\x00"
        + (len(pdu) + 1).to_bytes(2, "big")
        + bytes([unit_id])
        + pdu
    )


# --- the ADV handshake -------------------------------------------------------


def test_advertisement_is_parsed_from_the_real_banner() -> None:
    # The captured pilot banner carries no terminator, which is exactly why
    # accepting one needs the caller to say the peer has gone quiet.
    advertisement, tail = p.extract_advertisement(REAL_ADVERTISEMENT, allow_unterminated=True)
    assert advertisement is not None
    assert advertisement.serial == "HV2340123456"
    assert advertisement.model == "SDongle-WLAN-FE"
    assert advertisement.software_version == "V100R001C00SPC124"
    assert advertisement.product == "P1"
    assert advertisement.aggregate_unit_id == 100
    assert advertisement.protocol_version == "1.3"
    assert tail == b""


def test_advertisement_unit_is_read_from_the_banner_not_assumed() -> None:
    banner = REAL_ADVERTISEMENT.replace(b"5=100", b"5=17") + b")"
    advertisement, _tail = p.extract_advertisement(banner)
    assert advertisement is not None and advertisement.aggregate_unit_id == 17


def test_advertisement_without_a_unit_field_falls_back_to_the_observed_unit() -> None:
    banner = b"ADV(J1=SDongle-WLAN-FE;4=HV2340123456;6=1.3)"
    advertisement, _tail = p.extract_advertisement(banner)
    assert advertisement is not None and advertisement.aggregate_unit_id == 100


def test_advertisement_without_a_serial_is_refused_once_the_peer_goes_quiet() -> None:
    with pytest.raises(p.HuaweiProtocolError, match="serial"):
        p.extract_advertisement(b"ADV(J1=SDongle-WLAN-FE;5=100;6=1.3)", allow_unterminated=True)


def test_a_banner_without_a_serial_is_merely_incomplete_while_bytes_may_follow() -> None:
    advertisement, _tail = p.extract_advertisement(b"ADV(J1=SDongle-WLAN-FE;5=100;6=1.3)")
    assert advertisement is None


def test_a_partial_banner_is_not_mistaken_for_a_complete_one() -> None:
    advertisement, tail = p.extract_advertisement(b"AD")
    assert advertisement is None and tail == b"AD"


def test_a_banner_still_arriving_is_incomplete_not_a_banner_without_a_serial() -> None:
    """The failure this rule exists for.

    `ADV(J1=...;2=...` on its own has the shape of a complete announcement
    that happens to carry no serial. Treating it as one would refuse every
    dongle whose banner crossed a TCP segment boundary -- which is most of
    them, since the listener reads whatever the network delivers.
    """
    half = REAL_ADVERTISEMENT[:30]
    advertisement, tail = p.extract_advertisement(half)
    assert advertisement is None and tail == half


def test_bytes_after_the_banner_are_handed_back_for_the_frame_buffer() -> None:
    frame = register_response(1, 100, aggregate_block())
    advertisement, tail = p.extract_advertisement(REAL_ADVERTISEMENT + b"\r\n" + frame)
    assert advertisement is not None
    # The terminator is not part of the banner and must not be swallowed either.
    assert tail.endswith(frame)


# --- framing -----------------------------------------------------------------


def test_a_frame_split_across_reads_is_reassembled() -> None:
    frame = register_response(7, 100, aggregate_block())
    buffer = p.FrameBuffer()
    for index in range(len(frame) - 1):
        assert buffer.feed(frame[index : index + 1]) == []
    frames = buffer.feed(frame[-1:])
    assert len(frames) == 1
    assert frames[0].transaction_id == 7 and frames[0].unit_id == 100


def test_three_frames_arriving_in_one_read_are_all_returned() -> None:
    stream = b"".join(register_response(index, 100, aggregate_block()) for index in range(1, 4))
    frames = p.FrameBuffer().feed(stream)
    assert [frame.transaction_id for frame in frames] == [1, 2, 3]


def test_a_trailing_partial_frame_is_kept_for_the_next_read() -> None:
    complete = register_response(1, 100, aggregate_block())
    partial = register_response(2, 100, aggregate_block())[:5]
    buffer = p.FrameBuffer()
    frames = buffer.feed(complete + partial)
    assert len(frames) == 1
    assert buffer.pending == partial
    assert len(buffer.feed(register_response(2, 100, aggregate_block())[5:])) == 1


def test_a_non_modbus_protocol_id_is_refused_rather_than_buffered() -> None:
    frame = bytearray(register_response(1, 100, aggregate_block()))
    frame[2:4] = (9).to_bytes(2, "big")
    with pytest.raises(p.HuaweiProtocolError, match="protocol id"):
        p.FrameBuffer().feed(bytes(frame))


@pytest.mark.parametrize("length", [0, 1, p.MAX_MBAP_LENGTH + 1, 0xFFFF])
def test_an_out_of_range_mbap_length_is_refused(length: int) -> None:
    frame = bytearray(register_response(1, 100, aggregate_block()))
    frame[4:6] = length.to_bytes(2, "big")
    with pytest.raises(p.HuaweiProtocolError, match="length"):
        p.FrameBuffer().feed(bytes(frame))


# --- validation of an answer against its request -----------------------------


def test_a_stale_transaction_id_is_refused() -> None:
    frame = p.FrameBuffer().feed(register_response(4, 100, aggregate_block()))[0]
    with pytest.raises(p.HuaweiProtocolError, match="Transaction id"):
        p.read_register_payload(frame, expected_transaction_id=5, expected_unit_id=100, expected_quantity=p.AGGREGATE_BLOCK_QUANTITY)


def test_an_answer_about_another_unit_is_refused() -> None:
    frame = p.FrameBuffer().feed(register_response(4, 1, aggregate_block()))[0]
    with pytest.raises(p.HuaweiProtocolError, match="unit"):
        p.read_register_payload(frame, expected_transaction_id=4, expected_unit_id=100, expected_quantity=p.AGGREGATE_BLOCK_QUANTITY)


def test_a_byte_count_that_disagrees_with_the_request_is_refused() -> None:
    frame = p.FrameBuffer().feed(register_response(4, 100, aggregate_block()[:20]))[0]
    with pytest.raises(p.HuaweiProtocolError, match="declares"):
        p.read_register_payload(frame, expected_transaction_id=4, expected_unit_id=100, expected_quantity=p.AGGREGATE_BLOCK_QUANTITY)


def test_the_inverter_refusal_seen_on_both_pilots_decodes_as_a_named_answer() -> None:
    frame = p.FrameBuffer().feed(exception_response(9, 1, p.FUNCTION_READ_HOLDING_REGISTERS, 0x04))[0]
    assert frame.is_exception
    with pytest.raises(p.ModbusExceptionResponse) as raised:
        p.read_register_payload(frame, expected_transaction_id=9, expected_unit_id=1, expected_quantity=2)
    assert raised.value.function == 0x83
    assert raised.value.exception_code == 0x04
    assert raised.value.name == "slave_device_failure"
    assert raised.value.is_downstream_failure


def test_a_different_exception_is_not_treated_as_a_downstream_failure() -> None:
    frame = p.FrameBuffer().feed(exception_response(9, 100, p.FUNCTION_READ_HOLDING_REGISTERS, 0x02))[0]
    with pytest.raises(p.ModbusExceptionResponse) as raised:
        p.read_register_payload(frame, expected_transaction_id=9, expected_unit_id=100, expected_quantity=2)
    assert raised.value.name == "illegal_data_address"
    assert not raised.value.is_downstream_failure


# --- value decoding ----------------------------------------------------------


def test_the_aggregate_block_decodes_to_kilowatts() -> None:
    body = aggregate_block()
    reading = p.parse_aggregated_block(body)
    assert reading.pv_input_power_kw == Decimal("5.432")
    assert reading.load_power_kw == Decimal("3.120")
    assert reading.grid_power_kw == Decimal("-2.312")
    assert reading.battery_power_kw == Decimal("0")
    assert reading.total_active_power_kw == Decimal("5.400")
    assert reading.signal_count == 5


def test_the_raw_registers_are_kept_beside_the_interpretation() -> None:
    reading = p.parse_aggregated_block(aggregate_block())
    assert reading.raw_registers[str(p.REGISTER_PV_INPUT_POWER)] == 5432
    assert reading.raw_registers[str(p.REGISTER_GRID_POWER)] == -2312


def test_a_signed_register_keeps_its_sign_through_the_scale() -> None:
    reading = p.parse_aggregated_block(aggregate_block(grid=-1, battery=-987654))
    assert reading.grid_power_kw == Decimal("-0.001")
    assert reading.battery_power_kw == Decimal("-987.654")


def test_sentinels_decode_to_none_and_never_to_a_full_scale_reading() -> None:
    reading = p.parse_aggregated_block(aggregate_block(pv=None, grid=None))
    assert reading.pv_input_power_kw is None
    assert reading.grid_power_kw is None
    assert reading.raw_registers[str(p.REGISTER_PV_INPUT_POWER)] is None
    # The signals that did answer are unaffected.
    assert reading.load_power_kw == Decimal("3.120")
    assert reading.signal_count == 3


def test_the_other_signed_sentinel_is_also_treated_as_absent() -> None:
    body = bytearray(aggregate_block())
    offset = (p.REGISTER_BATTERY_POWER - p.AGGREGATE_BLOCK_START) * 2
    body[offset : offset + 4] = (0x80000000).to_bytes(4, "big")
    assert p.parse_aggregated_block(bytes(body)).battery_power_kw is None


def test_a_block_that_does_not_reach_the_last_signal_is_refused() -> None:
    with pytest.raises(p.HuaweiProtocolError, match="does not cover"):
        p.parse_aggregated_block(aggregate_block()[:20])


def test_scaling_is_exact_decimal_arithmetic() -> None:
    # 0.1 + 0.2 == 0.3 has to hold, because these values are integrated into
    # billable energy downstream.
    assert p.scaled(100) + p.scaled(200) == p.scaled(300)
    assert p.scaled(None) is None


def test_ascii_register_blocks_stop_at_the_padding() -> None:
    assert p.decode_ascii_registers(b"V100R001C00SPC124\x00\x00\x00") == "V100R001C00SPC124"


# --- request building --------------------------------------------------------


def test_a_read_request_is_a_well_formed_mbap_frame() -> None:
    request = p.build_read_holding_registers(transaction_id=513, unit_id=100, address=37498, quantity=20)
    assert request == bytes.fromhex("0201") + b"\x00\x00" + bytes.fromhex("0006") + bytes([100]) + bytes.fromhex("03927A0014")


def test_the_device_identification_probe_is_the_observed_huawei_sequence() -> None:
    request = p.build_read_device_identification(transaction_id=1, unit_id=100)
    assert request.endswith(bytes.fromhex("2B0E0387"))


def test_request_builders_refuse_values_that_cannot_fit_on_the_wire() -> None:
    with pytest.raises(p.HuaweiProtocolError):
        p.build_read_holding_registers(transaction_id=0x10000, unit_id=100, address=1, quantity=1)
    with pytest.raises(p.HuaweiProtocolError):
        p.build_read_holding_registers(transaction_id=1, unit_id=100, address=1, quantity=126)


def test_this_module_can_only_ever_build_a_read() -> None:
    """The read-only guarantee, asserted structurally rather than promised.

    Every frame this module can produce carries function 0x03 or 0x2B. There
    is no encoder for 0x06 or 0x10, so no amount of misuse further up can turn
    this integration into something that commands an inverter.
    """
    assert sorted(name for name in dir(p) if name.startswith("build_")) == [
        "build_read_device_identification",
        "build_read_holding_registers",
    ]
    assert [name for name in dir(p) if "write" in name.lower()] == []
    frames = (
        p.build_read_holding_registers(transaction_id=1, unit_id=100, address=37498, quantity=2),
        p.build_read_device_identification(transaction_id=1, unit_id=100),
    )
    assert {frame[7] for frame in frames} == {p.FUNCTION_READ_HOLDING_REGISTERS, p.FUNCTION_READ_DEVICE_IDENTIFICATION}


def test_device_identification_objects_decode() -> None:
    objects = bytes([0x87, 0x0C]) + b"HV2340123456"
    # MEI | read-device-id code | conformity | more-follows | next-object | count
    header = bytes([p.MEI_TYPE_DEVICE_IDENTIFICATION, 0x03, 0x01, 0x00, 0x00, 0x01])
    pdu = bytes([p.FUNCTION_READ_DEVICE_IDENTIFICATION]) + header + objects
    frame = p.FrameBuffer().feed(
        (3).to_bytes(2, "big") + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + bytes([100]) + pdu
    )[0]
    decoded = p.read_device_identification_payload(frame, expected_transaction_id=3, expected_unit_id=100)
    assert decoded == {0x87: "HV2340123456"}
