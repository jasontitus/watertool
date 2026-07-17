from datetime import timedelta

from watertool.db.store import Store
from watertool.rachio import events as ev
from watertool.rachio.models import Device
from watertool.util import utcnow

DEVICE = {
    "id": "d1", "name": "Home", "on": True,
    "zones": [{
        "id": "z1", "zoneNumber": 1, "name": "Front Lawn", "enabled": True,
        "yardAreaSquareFeet": 1000, "efficiency": 0.7,
        "customNozzle": {"inchesPerHour": 1.0, "name": "Fixed Spray"},
    }],
}


def _seed(store: Store) -> None:
    store.init_db()
    d = Device.from_api(DEVICE)
    store.upsert_property("dev:d1", d.name, None, None, None)
    store.upsert_device(d, "dev:d1")
    for z in d.zones:
        store.upsert_zone(z)


def _evt(kind, ts, duration=None, flow=None, sig=None, eid=None):
    return ev.NormalizedEvent(
        source="poll", kind=kind, device_id="d1", zone_id="z1", zone_number=1,
        zone_name="Front Lawn", timestamp=ts, duration_seconds=duration,
        flow_volume_gallons=flow, schedule_id=None,
        event_id=eid or f"poll:{kind}:{ts.isoformat()}",
    ), sig


def test_init_and_reprocess_builds_run_with_gallons():
    store = Store(":memory:")
    _seed(store)
    start = utcnow() - timedelta(hours=2)
    for e, sig in [
        _evt(ev.STARTED, start, duration=600),
        _evt(ev.COMPLETED, start + timedelta(seconds=600), duration=600, flow=None),
    ]:
        store.record_event(e, signature_ok=sig)

    n = store.reprocess_device_runs("d1", lookback_days=None)
    assert n == 1
    with store._conn() as c:
        row = c.execute("SELECT * FROM zone_runs").fetchone()
    assert row["duration_seconds"] == 600
    assert row["complete"] == 1
    # 1 in/hr * (600/3600) h * 1000 sqft * 0.6233
    assert abs(row["gallons_estimated"] - 103.883) < 0.01


def test_record_event_dedup():
    store = Store(":memory:")
    _seed(store)
    e, _ = _evt(ev.STARTED, utcnow(), duration=600, eid="poll:fixed")
    assert store.record_event(e, signature_ok=None) is True
    assert store.record_event(e, signature_ok=None) is False


def test_unverified_events_excluded_from_runs():
    store = Store(":memory:")
    _seed(store)
    e, _ = _evt(ev.COMPLETED, utcnow() - timedelta(minutes=5), duration=600)
    store.record_event(e, signature_ok=False)  # forged / failed signature
    n = store.reprocess_device_runs("d1", lookback_days=None)
    assert n == 0
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM zone_runs").fetchone()["n"] == 0
        # ...but it IS retained in the ledger for forensics
        assert c.execute("SELECT COUNT(*) AS n FROM events_raw").fetchone()["n"] == 1


def test_reprocess_is_idempotent():
    store = Store(":memory:")
    _seed(store)
    start = utcnow() - timedelta(hours=1)
    for e, sig in [
        _evt(ev.STARTED, start, duration=600),
        _evt(ev.COMPLETED, start + timedelta(seconds=600), duration=600),
    ]:
        store.record_event(e, signature_ok=sig)
    store.reprocess_device_runs("d1", lookback_days=None)
    store.reprocess_device_runs("d1", lookback_days=None)
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM zone_runs").fetchone()["n"] == 1


def test_weekly_usage_report():
    store = Store(":memory:")
    _seed(store)
    start = utcnow() - timedelta(days=1)
    for e, sig in [
        _evt(ev.STARTED, start, duration=600),
        _evt(ev.COMPLETED, start + timedelta(seconds=600), duration=600),
    ]:
        store.record_event(e, signature_ok=sig)
    store.reprocess_device_runs("d1", lookback_days=None)
    rows = store.weekly_usage(weeks=8)
    assert len(rows) == 1
    assert rows[0]["zone"] == "Front Lawn"
    assert rows[0]["gallons"] > 100
