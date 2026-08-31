"""Recognising Huawei data collectors in a FusionSolar device list.

A dongle is identified to this integration by the serial it announces, and
somebody has to know that serial before the dongle ever dials in -- otherwise
every new installation spends its first sessions in quarantine. Typing 61
serials by hand is the one step in the whole rollout that the source policy
cannot protect: a transposed character silently attributes one customer's
production to another.

FusionSolar already knows every serial. The problem is getting at it: on the
pilot account `getDevList` succeeds **exactly once per hour** (observed
2026-08-27 -- successes at 06:32, 07:32, 08:32, and `failCode 407` for every
attempt in between, regardless of batch size). A separate discovery call
cannot win that slot, because the device-status poll already takes it.

So this module does not make a call. It reads the rows the poll *already*
fetched and throws away, which cost nothing extra and arrive every hour.

Classification is by the model and name text, never by `devTypeId`: the
numeric type ids for dongles and loggers are not documented anywhere this
repository can point at, and a guessed one would map the wrong device. Every
row's real `devTypeId` is carried through instead, so one live run turns the
guess into evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

# Both classes of Huawei collector. A SmartLogger serves a different register
# map from an SDongle -- `listener.py` records that rather than assuming it --
# but both announce themselves the same way, so both are worth discovering.
LOGGER_MARKERS = ("smartlogger",)
DONGLE_MARKERS = ("sdongle", "dongle")
_TEXT_FIELDS = ("devName", "devTypeName", "model", "devModel", "invType", "name")


@dataclass(frozen=True)
class DiscoveredCollector:
    """One dongle or logger, as FusionSolar describes it."""

    kind: str
    serial: str
    station_code: str
    asset_id: int | None
    device_name: str | None
    model: str | None
    dev_type_id: Any

    @property
    def is_mappable(self) -> bool:
        """A collector is only useful once it has both a serial and a plant."""
        return bool(self.serial) and self.asset_id is not None


def classify(row: dict) -> str | None:
    """`logger`, `dongle`, or None for anything that is not a collector."""
    text = " ".join(str(row.get(field) or "") for field in _TEXT_FIELDS).casefold()
    if any(marker in text for marker in LOGGER_MARKERS):
        return "logger"
    # "logger" on its own also means a logger, but only when nothing in the
    # row says dongle -- some dongle names carry both words.
    if any(marker in text for marker in DONGLE_MARKERS):
        return "dongle"
    if "logger" in text:
        return "logger"
    return None


def serial_of(row: dict) -> str:
    """The serial the collector will *announce*, not its cloud identifier.

    `esnCode`/`sn` is what appears in field 4 of the announcement and is
    therefore the only identifier a mapping can be keyed on. `devId` is the
    account's internal handle, and the device never says it on the wire.
    """
    return str(row.get("esnCode") or row.get("sn") or "").strip()


def collectors_in(
    rows: Iterable[dict], *, asset_by_station: dict[str, int] | None = None
) -> list[DiscoveredCollector]:
    """Every collector in a device list, resolved to its plant where possible."""
    resolved = asset_by_station or {}
    found: list[DiscoveredCollector] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = classify(row)
        if kind is None:
            continue
        station = str(row.get("stationCode") or row.get("stationDn") or "").strip()
        found.append(
            DiscoveredCollector(
                kind=kind,
                serial=serial_of(row),
                station_code=station,
                asset_id=resolved.get(station),
                device_name=str(row.get("devName") or "") or None,
                model=str(row.get("model") or row.get("devModel") or row.get("devTypeName") or "") or None,
                dev_type_id=row.get("devTypeId"),
            )
        )
    return found


def as_metadata(collectors: Iterable[DiscoveredCollector]) -> dict[str, Any]:
    """The block a device-status sync run carries so this is readable later.

    Stored on the run that actually made the call, which is the honest place
    for it: the provenance of "we saw these serials" is "this request, at this
    time", and nothing has to be re-fetched to read it back.
    """
    items = [asdict(collector) for collector in collectors]
    for item in items:
        item["dev_type_id"] = None if item["dev_type_id"] is None else str(item["dev_type_id"])
    return {
        "collectors": items,
        "collector_count": len(items),
        "collector_kinds": sorted({item["kind"] for item in items}),
        "collector_dev_type_ids": sorted({item["dev_type_id"] for item in items if item["dev_type_id"]}),
    }
