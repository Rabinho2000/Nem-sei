"""Finding Huawei collectors in a device list FusionSolar already returned.

The rows here follow the shape the real account shows for the pilot plant:
a `Dongle-TA2430080460` of model `SDongleA-05` beside an inverter of model
`SUN2000-20KTL-M5`. Both were read off the live device list on 2026-08-27.
"""
from __future__ import annotations

from nemsei.integrations.huawei_scada.fusionsolar_discovery import (
    as_metadata,
    classify,
    collectors_in,
    serial_of,
)

DONGLE = {
    "devName": "Dongle-TA2430080460",
    "devTypeName": "Dongle",
    "model": "SDongleA-05",
    "esnCode": "TA2430080460",
    "devTypeId": 62,
    "stationCode": "NE=140313444",
}
INVERTER = {
    "devName": "Inverter 1",
    "devTypeName": "Inverter",
    "model": "SUN2000-20KTL-M5",
    "esnCode": "TA2310347396",
    "devTypeId": 1,
    "stationCode": "NE=140313444",
}
LOGGER = {
    "devName": "SmartLogger3000",
    "devTypeName": "SmartLogger",
    "model": "SmartLogger3000A01EU",
    "esnCode": "SL2340001122",
    "devTypeId": 47,
    "stationCode": "NE=999",
}


def test_a_dongle_is_recognised_from_what_the_account_calls_it() -> None:
    assert classify(DONGLE) == "dongle"


def test_a_smartlogger_is_recognised_as_a_logger_not_a_dongle() -> None:
    assert classify(LOGGER) == "logger"


def test_an_inverter_is_not_a_collector() -> None:
    assert classify(INVERTER) is None


def test_the_serial_is_the_one_the_device_announces_not_its_cloud_id() -> None:
    """`devId` is the account's handle; the dongle never says it on the wire."""
    assert serial_of(DONGLE) == "TA2430080460"
    assert serial_of({"sn": "TA9", "devId": "1000000182491014"}) == "TA9"
    assert serial_of({"devId": "1000000182491014"}) == ""


def test_collectors_resolve_to_the_plant_their_station_code_points_at() -> None:
    found = collectors_in([DONGLE, INVERTER, LOGGER], asset_by_station={"NE=140313444": 94})
    assert [item.kind for item in found] == ["dongle", "logger"]
    dongle = found[0]
    assert dongle.serial == "TA2430080460"
    assert dongle.asset_id == 94
    assert dongle.is_mappable


def test_a_collector_whose_plant_is_out_of_scope_is_reported_but_not_mappable() -> None:
    """Seen, named, and explicitly not ready -- never mapped to a guess."""
    found = collectors_in([LOGGER], asset_by_station={"NE=140313444": 94})
    assert found[0].asset_id is None
    assert not found[0].is_mappable


def test_a_collector_without_a_serial_is_not_mappable() -> None:
    nameless = {**DONGLE, "esnCode": "", "sn": ""}
    found = collectors_in([nameless], asset_by_station={"NE=140313444": 94})
    assert found[0].serial == ""
    assert not found[0].is_mappable


def test_rows_that_are_not_dictionaries_are_skipped_rather_than_raising() -> None:
    assert collectors_in([DONGLE, None, "junk", 7]) != []


def test_the_metadata_block_carries_the_type_ids_rather_than_assuming_them() -> None:
    """One live run turns the guess about `devTypeId` into evidence."""
    payload = as_metadata(collectors_in([DONGLE, LOGGER], asset_by_station={"NE=140313444": 94}))
    assert payload["collector_count"] == 2
    assert payload["collector_kinds"] == ["dongle", "logger"]
    assert payload["collector_dev_type_ids"] == ["47", "62"]
    assert payload["collectors"][0]["serial"] == "TA2430080460"


def test_the_metadata_block_is_json_safe() -> None:
    """It is written straight into `SyncRun.metadata_json`."""
    import json

    payload = as_metadata(collectors_in([DONGLE, LOGGER]))
    assert json.loads(json.dumps(payload))["collector_count"] == 2
