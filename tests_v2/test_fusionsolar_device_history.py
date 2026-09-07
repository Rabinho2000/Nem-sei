"""The `getDevHistoryKpi` client contract, isolated from any network.

Pinned against the **real** response shape captured from the live account on
2026-09-06 (`docs/v2/FUSIONSOLAR_DEVICE_HISTORY.md`): a flat `data` list of
`{devId, sn, collectTime, dataItemMap}` rows at 5-minute granularity.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from nemsei.integrations.fusionsolar.client import (
    FusionSolarClient,
    FusionSolarClientError,
    FusionSolarCredentials,
    HttpResponse,
)
from nemsei.integrations.fusionsolar.device_history import DEVICE_HISTORY_BATCH, day_window_ms


class RecordingTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, payload, headers, timeout_seconds):
        self.calls.append((url, payload))
        return HttpResponse(status_code=200, payload=self.payload, headers={"XSRF-TOKEN": "t"})


def _client(payload):
    transport = RecordingTransport(payload)
    client = FusionSolarClient(
        credentials=FusionSolarCredentials(base_url="https://eu5.fusionsolar.huawei.com", username="u", password="p"),
        transport=transport,
    )
    client._token = "t"
    return client, transport


# Sanitised: the identifiers are synthetic, the *shape* is what was captured
# from the live account on 2026-09-06 (see docs/v2/FUSIONSOLAR_DEVICE_HISTORY.md).
# Pinning the shape is the point of this fixture; a real inverter serial adds
# nothing to it.
_CAPTURED_ROW = {
    "devId": 1000000000000001,
    "sn": "SN-TEST-0000001",
    "collectTime": 1788480000000,
    "dataItemMap": {"active_power": 0.0, "inverter_state": 40960.0, "day_cap": 0.0, "efficiency": 0.0},
}


def test_posts_the_v1_payload_shape_to_the_verified_endpoint() -> None:
    client, transport = _client({"success": True, "failCode": 0, "data": [_CAPTURED_ROW]})
    rows = client.device_history_batch(["A", "B"], device_type_id=1, start_time_ms=1788480000000, end_time_ms=1788566399999)
    url, payload = transport.calls[0]
    assert url.endswith("/thirdData/getDevHistoryKpi")
    assert payload == {"devIds": "A,B", "devTypeId": 1, "startTime": 1788480000000, "endTime": 1788566399999}
    assert rows == [_CAPTURED_ROW]


def test_refuses_more_than_ten_devices() -> None:
    """V1's own chunk size; one day of ten devices is already ~2 880 rows."""
    client, _transport = _client({"success": True, "data": []})
    assert DEVICE_HISTORY_BATCH == 10
    with pytest.raises(FusionSolarClientError):
        client.device_history_batch([str(index) for index in range(11)], device_type_id=1, start_time_ms=1, end_time_ms=2)


def test_refuses_an_empty_batch_and_an_inverted_window() -> None:
    client, _transport = _client({"success": True, "data": []})
    with pytest.raises(FusionSolarClientError):
        client.device_history_batch([], device_type_id=1, start_time_ms=1, end_time_ms=2)
    with pytest.raises(FusionSolarClientError):
        client.device_history_batch(["A"], device_type_id=1, start_time_ms=2, end_time_ms=1)


def test_requires_authentication_first() -> None:
    client = FusionSolarClient(
        credentials=FusionSolarCredentials(base_url="https://x", username="u", password="p"),
        transport=RecordingTransport({}),
    )
    with pytest.raises(FusionSolarClientError):
        client.device_history_batch(["A"], device_type_id=1, start_time_ms=1, end_time_ms=2)


def test_a_provider_failure_is_surfaced_not_swallowed() -> None:
    client, _transport = _client({"success": False, "failCode": 407, "message": "ACCESS_FREQUENCY_IS_TOO_HIGH"})
    with pytest.raises(FusionSolarClientError):
        client.device_history_batch(["A"], device_type_id=1, start_time_ms=1, end_time_ms=2)


def test_a_non_list_data_is_rejected_rather_than_coerced() -> None:
    client, _transport = _client({"success": True, "failCode": 0, "data": {"unexpected": "shape"}})
    with pytest.raises(FusionSolarClientError):
        client.device_history_batch(["A"], device_type_id=1, start_time_ms=1, end_time_ms=2)


def test_day_window_is_built_in_the_given_zone_not_the_process_zone() -> None:
    """V1 built this window in process-local time; V2 makes it explicit."""
    utc_start, utc_end = day_window_ms(date(2026, 9, 4), ZoneInfo("UTC"))
    lisbon_start, lisbon_end = day_window_ms(date(2026, 9, 4), ZoneInfo("Europe/Lisbon"))
    assert utc_start != lisbon_start, "September is WEST (UTC+1) in Lisbon"
    assert datetime.fromtimestamp(utc_start / 1000, timezone.utc).isoformat() == "2026-09-04T00:00:00+00:00"
    assert utc_end - utc_start == 24 * 60 * 60 * 1000 - 1


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 9, 4), 24)])
def test_day_window_respects_dst_transitions(day: date, hours: int) -> None:
    """A DST day is 23 or 25 hours wide, never a fixed 24."""
    start, end = day_window_ms(day, ZoneInfo("Europe/Lisbon"))
    assert (end - start + 1) / (60 * 60 * 1000) == hours


def test_the_real_captured_row_normalizes_through_the_existing_device_normalizer() -> None:
    """History rows are `getDevRealKpi` rows in shape, which is why the
    contractual path reuses the realtime normalizer instead of a second one."""
    from nemsei.integrations.fusionsolar.device_status import (
        FusionSolarDeviceContract,
        normalize_device_realtime_row,
    )

    sample = normalize_device_realtime_row(
        _CAPTURED_ROW,
        expected_external_ids=frozenset({"1000000000000001"}),
        contract=FusionSolarDeviceContract(active_power_unit="kW", day_energy_unit="kWh"),
        ingested_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert sample is not None
    assert sample.external_device_id == "1000000000000001"
    assert sample.observed_at == datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    assert sample.raw_inverter_state == "40960.0"
    assert float(sample.active_power_kw) == 0.0
