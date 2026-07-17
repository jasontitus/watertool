import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from watertool.config import Settings
from watertool.db.store import Store
from watertool.ingest.receiver import create_app
from watertool.rachio.models import Device
from watertool.util import utcnow

DEVICE = {
    "id": "d1", "name": "Home", "on": True,
    "zones": [{"id": "z1", "zoneNumber": 1, "name": "Front Lawn", "enabled": True,
               "yardAreaSquareFeet": 1000,
               "customNozzle": {"inchesPerHour": 1.0, "name": "Spray"}}],
}


def _seeded_store() -> Store:
    store = Store(":memory:")
    store.init_db()
    d = Device.from_api(DEVICE)
    store.upsert_property("dev:d1", d.name, None, None, None)
    store.upsert_device(d, "dev:d1")
    for z in d.zones:
        store.upsert_zone(z)
    return store


def _completed_body(event_id: str = "e1") -> dict:
    return {
        "id": event_id, "subType": "ZONE_COMPLETED", "deviceId": "d1",
        "zoneId": "z1", "zoneNumber": 1, "zoneName": "Front Lawn",
        "duration": 600, "timestamp": utcnow().isoformat(),
    }


def test_health():
    settings = Settings(rachio_api_token="tok", webhook_verify="none")
    app = create_app(settings, _seeded_store())
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_webhook_none_mode_creates_run():
    settings = Settings(rachio_api_token="tok", webhook_verify="none")
    store = _seeded_store()
    client = TestClient(create_app(settings, store))
    r = client.post("/webhooks/rachio", json=_completed_body())
    assert r.status_code == 200
    data = r.json()
    assert data["verified"] is True and data["new"] == 1
    with store._conn() as c:
        row = c.execute("SELECT * FROM zone_runs").fetchone()
    assert row is not None
    assert row["complete"] == 1
    assert abs(row["gallons_estimated"] - 103.883) < 0.01


def test_webhook_hmac_valid_signature_creates_run():
    settings = Settings(rachio_api_token="tok", webhook_verify="hmac")
    store = _seeded_store()
    client = TestClient(create_app(settings, store))

    raw = json.dumps(_completed_body("hmac-ok")).encode()
    sig = hmac.new(b"tok", raw, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/rachio", content=raw,
                    headers={"x-signature": sig, "content-type": "application/json"})
    assert r.json()["verified"] is True
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM zone_runs").fetchone()["n"] == 1


def test_webhook_hmac_bad_signature_stores_but_no_run():
    settings = Settings(rachio_api_token="tok", webhook_verify="hmac")
    store = _seeded_store()
    client = TestClient(create_app(settings, store))

    raw = json.dumps(_completed_body("hmac-bad")).encode()
    r = client.post("/webhooks/rachio", content=raw,
                    headers={"x-signature": "deadbeef", "content-type": "application/json"})
    assert r.status_code == 200  # still 2xx so Rachio doesn't deregister us
    assert r.json()["verified"] is False
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM zone_runs").fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) AS n FROM events_raw").fetchone()["n"] == 1
