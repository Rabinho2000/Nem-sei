from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from monitoring_board.services.fusionsolar import map_fusionsolar_status
from monitoring_board.services.sigenergy_models import map_sigenergy_status


EDITABLE_ASSET_FIELDS = (
    "project_name",
    "company_name",
    "address",
    "location",
    "country",
    "timezone",
    "kwp",
    "kwac",
    "commissioning_date",
)


def first_value(payloads: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
    return None


def text_value(value: Any) -> str:
    return str(value or "").strip()


def float_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def normalize_power_to_kw(value: Any) -> float | None:
    parsed = float_value(value)
    if parsed is None:
        return None
    return parsed / 1000 if parsed > 1000 else parsed


def date_value(value: Any) -> str:
    raw = text_value(value)
    if not raw:
        return ""
    if raw.isdigit():
        try:
            timestamp = int(raw)
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            return datetime.fromtimestamp(timestamp).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return ""


def _status_from_fusionsolar(realtime: dict[str, Any]) -> str:
    data_map = realtime.get("dataItemMap") if isinstance(realtime.get("dataItemMap"), dict) else realtime
    return map_fusionsolar_status(
        first_value(
            [data_map],
            ("real_health_state", "healthState", "runningState", "status"),
        )
    )


def normalize_fusionsolar_import(
    station: dict[str, Any],
    realtime: dict[str, Any] | None,
    devices: list[dict[str, Any]],
    device_realtime: dict[str, dict[str, Any]] | None = None,
    alarms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    realtime = realtime or {}
    data_map = realtime.get("dataItemMap") if isinstance(realtime.get("dataItemMap"), dict) else realtime
    device_realtime = device_realtime or {}
    external_id = text_value(first_value([station, realtime], ("plantCode", "stationCode", "stationId")))
    external_name = text_value(first_value([station], ("plantName", "stationName", "name"))) or external_id
    latitude = float_value(first_value([station], ("latitude", "lat", "stationLatitude")))
    longitude = float_value(first_value([station], ("longitude", "lng", "lon", "stationLongitude")))
    normalized_devices: list[dict[str, Any]] = []
    for device in devices:
        identity = text_value(first_value([device], ("devId", "id", "devDn", "deviceDn", "esnCode", "sn")))
        live = device_realtime.get(identity, {})
        live_data = (
            live.get("dataItemMap")
            if isinstance(live.get("dataItemMap"), dict)
            else live
        )
        normalized_devices.append(
            {
                "external_device_id": identity,
                "name": text_value(first_value([device], ("devName", "deviceName", "name"))),
                "serial_number": text_value(first_value([device], ("esnCode", "sn", "serialNumber"))),
                "model": text_value(first_value([device], ("model", "devModel", "deviceModel", "invType"))),
                "rated_power_kw": normalize_power_to_kw(
                    first_value([device], ("ratedPower", "rated_power", "capacity", "nominalPower"))
                ),
                "status": text_value(
                    first_value(
                        [live_data, live, device],
                        (
                            "inverter_state",
                            "inverterState",
                            "status",
                            "runningState",
                        ),
                    )
                ),
                "device_type": text_value(first_value([device], ("devTypeId", "dev_type_id", "deviceTypeId"))),
                "payload": {"device": device, "realtime": live},
            }
        )
    return {
        "provider": "FusionSolar",
        "source_label": "FusionSolar API",
        "external_id": external_id,
        "external_name": external_name,
        "project_name": external_name,
        "company_name": text_value(
            first_value([station], ("ownerName", "organizationName", "companyName", "accountName"))
        ),
        "address": text_value(first_value([station], ("plantAddress", "stationAddress", "address"))),
        "location": text_value(first_value([station], ("city", "location", "regionName", "province"))),
        "country": text_value(first_value([station], ("country", "countryName", "countryCode"))),
        "timezone": text_value(first_value([station], ("timeZone", "timezone", "stationTimeZone"))),
        "latitude": latitude,
        "longitude": longitude,
        "kwp": normalize_power_to_kw(
            first_value(
                [station],
                ("capacity", "installedCapacity", "pvCapacity", "dcCapacity"),
            )
        ),
        "kwac": normalize_power_to_kw(
            first_value([station], ("acCapacity", "gridConnectionCapacity"))
        ),
        "commissioning_date": date_value(
            first_value([station], ("gridConnectionDate", "commissioningDate", "connectTime", "createTime"))
        ),
        "operational_status": _status_from_fusionsolar(realtime),
        "inverter_count": len(normalized_devices),
        "devices": normalized_devices,
        "pv_power_kw": float_value(first_value([data_map], ("active_power", "PVPower", "pvPower"))),
        "load_power_kw": float_value(first_value([data_map], ("load_power", "loadPower"))),
        "grid_power_kw": float_value(first_value([data_map], ("grid_power", "gridPower"))),
        "battery_power_kw": float_value(first_value([data_map], ("charge_power", "batteryPower"))),
        "battery_soc_pct": float_value(first_value([data_map], ("battery_soc", "batterySoc"))),
        "battery_capacity_kwh": float_value(first_value([station], ("batteryCapacity",))),
        "ev_power_kw": float_value(first_value([data_map], ("evPower",))),
        "heat_pump_power_kw": float_value(first_value([data_map], ("heatPumpPower",))),
        "alarm_count": len(alarms or []),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_sigenergy_import(
    system: dict[str, Any],
    energy_flow: dict[str, Any] | None,
) -> dict[str, Any]:
    energy_flow = energy_flow or {}
    external_id = text_value(first_value([system, energy_flow], ("systemId", "id", "stationId", "plantId")))
    external_name = text_value(first_value([system], ("systemName", "name", "stationName", "plantName"))) or external_id
    raw_status = first_value([system, energy_flow], ("systemStatus", "status", "runningStatus", "state"))
    return {
        "provider": "Sigenergy",
        "source_label": "Sigenergy API",
        "external_id": external_id,
        "external_name": external_name,
        "project_name": external_name,
        "company_name": text_value(
            first_value([system], ("ownerName", "organizationName", "companyName", "accountName"))
        ),
        "address": text_value(first_value([system], ("address", "systemAddress", "stationAddress"))),
        "location": text_value(first_value([system], ("city", "location", "regionName", "province"))),
        "country": text_value(first_value([system], ("country", "countryName", "countryCode"))),
        "timezone": text_value(first_value([system], ("timeZone", "timezone", "systemTimeZone"))),
        "latitude": float_value(first_value([system], ("latitude", "lat"))),
        "longitude": float_value(first_value([system], ("longitude", "lng", "lon"))),
        "kwp": normalize_power_to_kw(
            first_value(
                [system, energy_flow],
                ("pvCapacity", "installedCapacity", "dcCapacity"),
            )
        ),
        "kwac": normalize_power_to_kw(
            first_value([system], ("acCapacity", "ratedPower"))
        ),
        "commissioning_date": date_value(
            first_value([system], ("gridConnectionDate", "commissioningDate", "connectTime", "createTime"))
        ),
        "operational_status": map_sigenergy_status(raw_status),
        "inverter_count": int(float(first_value([system], ("inverterCount", "deviceCount")) or 0)),
        "devices": [],
        "pv_power_kw": float_value(first_value([energy_flow], ("pvPower",))),
        "load_power_kw": float_value(first_value([energy_flow], ("loadPower",))),
        "grid_power_kw": float_value(first_value([energy_flow], ("gridPower",))),
        "battery_power_kw": float_value(first_value([energy_flow], ("batteryPower",))),
        "battery_soc_pct": float_value(first_value([energy_flow], ("batterySoc",))),
        "battery_capacity_kwh": float_value(first_value([system, energy_flow], ("batteryCapacity",))),
        "ev_power_kw": float_value(first_value([energy_flow], ("evPower",))),
        "heat_pump_power_kw": float_value(first_value([energy_flow], ("heatPumpPower",))),
        "alarm_count": 0,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def missing_asset_fields(normalized: dict[str, Any]) -> list[str]:
    labels = {
        "project_name": "Nome",
        "company_name": "Proprietário/empresa",
        "address": "Morada",
        "location": "Localidade",
        "country": "País",
        "timezone": "Fuso horário",
        "kwp": "Potência DC",
        "kwac": "Potência AC",
        "commissioning_date": "Data de comissionamento",
    }
    return [labels[field] for field in EDITABLE_ASSET_FIELDS if normalized.get(field) in (None, "")]
