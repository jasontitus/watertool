"""Typed views over the Rachio account tree.

Rachio's `GET /public/person/{id}` returns a deeply nested JSON blob. We only
lift out the fields watertool needs (identity + the zone config required to
estimate gallons) and keep the raw dict for anything else. Parsing is tolerant:
missing keys become None rather than raising, because Rachio's payloads vary by
controller generation and firmware.

Note: we deliberately do NOT read or store the account holder's name/email — only
device and zone data. Keeping PII out of the store is intentional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class Zone:
    id: str
    device_id: str
    zone_number: int | None
    name: str | None
    enabled: bool | None
    area_sqft: float | None
    inches_per_hour: float | None
    efficiency: float | None
    soil: str | None
    crop: str | None
    nozzle_head: str | None
    image_url: str | None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict, device_id: str) -> "Zone":
        nozzle = d.get("customNozzle") or {}
        soil = d.get("customSoil") or {}
        crop = d.get("customCrop") or {}
        return cls(
            id=d["id"],
            device_id=device_id,
            zone_number=d.get("zoneNumber"),
            name=d.get("name"),
            enabled=d.get("enabled"),
            area_sqft=_num(d.get("yardAreaSquareFeet")),
            inches_per_hour=_num(nozzle.get("inchesPerHour")),
            efficiency=_num(d.get("efficiency")),
            soil=soil.get("name"),
            crop=crop.get("name"),
            nozzle_head=nozzle.get("name"),
            image_url=d.get("imageUrl"),
            raw=d,
        )


@dataclass
class Device:
    id: str
    name: str | None
    model: str | None
    serial_number: str | None
    mac_address: str | None
    latitude: float | None
    longitude: float | None
    timezone: str | None
    status: str | None
    on_standby: bool
    zones: list[Zone]
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Device":
        did = d["id"]
        zones = [Zone.from_api(z, did) for z in (d.get("zones") or [])]
        zones.sort(key=lambda z: (z.zone_number is None, z.zone_number))
        # Rachio `on` == True means the controller is active; False == standby.
        on = d.get("on")
        return cls(
            id=did,
            name=d.get("name"),
            model=d.get("model"),
            serial_number=d.get("serialNumber"),
            mac_address=d.get("macAddress"),
            latitude=_num(d.get("latitude")),
            longitude=_num(d.get("longitude")),
            timezone=d.get("timeZone"),
            status=d.get("status"),
            on_standby=(on is False),
            zones=zones,
            raw=d,
        )


def devices_from_person(person: dict) -> list[Device]:
    """Extract every controller from a `GET /public/person/{id}` response."""
    return [Device.from_api(d) for d in (person.get("devices") or [])]
