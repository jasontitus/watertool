"""End-to-end: mocked Rachio API -> backfill -> runs -> report, no token needed."""

from datetime import timedelta

import httpx

from watertool.config import Settings
from watertool.db.store import Store
from watertool.jobs.backfill import run_backfill
from watertool.rachio.client import RachioClient
from watertool.util import to_ms, utcnow

RUN_START = utcnow() - timedelta(days=1)

PERSON_TREE = {
    "id": "p1",
    "devices": [{
        "id": "d1", "name": "Lake House", "on": True, "model": "GEN3",
        "zones": [{
            "id": "z1", "zoneNumber": 1, "name": "Front Lawn", "enabled": True,
            "yardAreaSquareFeet": 1000, "efficiency": 0.75,
            "customNozzle": {"inchesPerHour": 1.0, "name": "Rotor"},
        }],
    }],
}

EVENTS = [
    {"id": "s1", "subType": "ZONE_STARTED", "zoneId": "z1", "zoneNumber": 1,
     "zoneName": "Front Lawn", "duration": 600, "eventDate": to_ms(RUN_START)},
    {"id": "c1", "subType": "ZONE_COMPLETED", "zoneId": "z1", "zoneNumber": 1,
     "zoneName": "Front Lawn", "duration": 600,
     "eventDate": to_ms(RUN_START + timedelta(seconds=600))},
]


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/public/person/info"):
        return httpx.Response(200, json={"id": "p1"})
    if path.endswith("/public/person/p1"):
        return httpx.Response(200, json=PERSON_TREE)
    if path.endswith("/event"):
        # Return the run's events for whichever chunk window asks; dedup handles
        # the repeats across the ~13 backfill windows.
        return httpx.Response(200, json=EVENTS)
    return httpx.Response(404, text=f"unhandled {path}")


def test_backfill_end_to_end():
    settings = Settings(rachio_api_token="tok", backfill_days=365)
    store = Store(":memory:")
    client = RachioClient("tok", min_interval=0, transport=httpx.MockTransport(_handler))

    result = run_backfill(client, store, settings, days=365)
    assert result["devices"] == 1
    assert result["runs"] == 1

    # The property/device/zone tree landed
    with store._conn() as c:
        assert c.execute("SELECT name FROM devices").fetchone()["name"] == "Lake House"
        run = c.execute("SELECT * FROM zone_runs").fetchone()
    assert run["complete"] == 1
    assert run["duration_seconds"] == 600
    assert abs(run["gallons_estimated"] - 103.883) < 0.01

    # Report aggregates it
    rows = store.weekly_usage(weeks=8)
    assert rows[0]["property"] == "Lake House"
    assert rows[0]["zone"] == "Front Lawn"
    assert rows[0]["runs"] == 1
