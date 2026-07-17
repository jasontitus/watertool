from datetime import timedelta

from fastapi.testclient import TestClient

from watertool.config import Settings
from watertool.db.store import Store
from watertool.ingest.receiver import create_app
from watertool.rachio import events as ev
from watertool.rachio.models import Device
from watertool.util import utcnow
from watertool.web.payload import anonymize_label, build_payload

# Fictional fixture data — never real addresses/coordinates.
DEVICE = {
    "id": "d1", "name": "Rachio-TEST01", "on": True, "status": "ONLINE",
    "zones": [{"id": "z1", "zoneNumber": 1, "name": "Front Lawn", "enabled": True,
               "yardAreaSquareFeet": 500, "customNozzle": {"inchesPerHour": 1.5}}],
}


def _seed() -> Store:
    store = Store(":memory:")
    store.init_db()
    d = Device.from_api(DEVICE)
    store.upsert_property("prop-1", "123 Oak Avenue", "123 Oak Avenue, Springfield, ST", 40.0, -80.0)
    store.upsert_device(d, "prop-1")
    for z in d.zones:
        store.upsert_zone(z)
    start = utcnow() - timedelta(days=2)
    for e, ts in [(ev.STARTED, start), (ev.COMPLETED, start + timedelta(seconds=600))]:
        store.record_event(ev.NormalizedEvent(
            source="poll", kind=e, device_id="d1", zone_id="z1", zone_number=1,
            zone_name=None, timestamp=ts, duration_seconds=600, flow_volume_gallons=None,
            schedule_id=None, event_id=f"poll:{e}:{ts.isoformat()}"), signature_ok=None)
    store.reprocess_device_runs("d1", lookback_days=None)
    return store


def test_anonymize_label_strips_house_number():
    assert anonymize_label("123 Oak Avenue") == "Oak Avenue"
    assert anonymize_label("742 Evergreen Terrace") == "Evergreen Terrace"
    assert anonymize_label(None) == "Property"


def test_payload_full_has_addresses():
    p = build_payload(_seed(), anonymize=False)
    assert p["anonymized"] is False
    prop = p["overview"][0]
    assert prop["name"] == "123 Oak Avenue"
    assert "Springfield" in prop["address"]
    assert prop["status"] == "ONLINE"
    assert prop["gallons"] > 0


def test_payload_anonymized_removes_pii():
    p = build_payload(_seed(), anonymize=True)
    assert p["anonymized"] is True
    prop = p["overview"][0]
    assert prop["name"] == "Oak Avenue"              # house number gone
    assert prop["address"] is None                    # address dropped
    assert prop["id"] == "p0"                          # internal id replaced
    assert all(c["name"] is None for c in prop["controllers"])
    # zone-level usage keeps only the anonymized label
    assert all("123" not in z["property"] for z in p["zones"])
    # zone names (garden labels) are retained — they're not location PII
    assert any(z["zone"] == "Front Lawn" for z in p["zones"])


def test_dashboard_routes():
    store = _seed()
    client = TestClient(create_app(Settings(rachio_api_token="tok"), store))
    r = client.get("/")
    assert r.status_code == 200 and "watertool" in r.text
    d = client.get("/data.json")
    assert d.status_code == 200
    body = d.json()
    assert body["anonymized"] is False  # live mode = real names locally
    assert {"overview", "monthly", "zones", "runs"} <= body.keys()
