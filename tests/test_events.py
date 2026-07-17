from datetime import datetime, timezone

from watertool.rachio import events as ev


def _dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# --- parsing -----------------------------------------------------------------

def test_parse_v2_webhook_completed():
    body = {
        "eventId": "abc-123",
        "eventType": "DEVICE_ZONE_RUN_COMPLETED_EVENT",
        "resourceId": "device-1",
        "timestamp": "2026-07-17T12:10:00Z",
        "payload": {"zoneId": "z1", "zoneNumber": 1, "zoneName": "Front Lawn",
                    "durationSeconds": 600, "flowVolume": 42.0},
    }
    events, provider = ev.parse_webhook(body)
    assert provider == "webhook_v2"
    e = events[0]
    assert e.kind == ev.COMPLETED
    assert e.device_id == "device-1"
    assert e.zone_id == "z1"
    assert e.duration_seconds == 600
    assert e.flow_volume_gallons == 42.0
    assert e.event_id == "webhook_v2:abc-123"


def test_parse_legacy_webhook_started():
    body = {
        "id": "evt-9",
        "type": "ZONE_STATUS",
        "subType": "ZONE_STARTED",
        "deviceId": "device-1",
        "zoneId": "z2",
        "zoneNumber": 2,
        "zoneName": "Back Beds",
        "durationInMinutes": 10,
        "timestamp": "2026-07-17T06:00:00Z",
    }
    events, provider = ev.parse_webhook(body)
    assert provider == "webhook_legacy"
    e = events[0]
    assert e.kind == ev.STARTED
    assert e.zone_id == "z2"
    assert e.duration_seconds == 600


def test_parse_poll_duration_from_summary_and_zone_resolution():
    events = [{
        "id": "p1",
        "subType": "ZONE_COMPLETED",
        "summary": "Front Lawn ran for 12 minutes",
        "eventDate": 1_752_753_000_000,
    }]
    zone_index = {"Front Lawn": {"id": "z1", "zoneNumber": 1}}
    parsed = ev.parse_poll_events(events, "device-1", zone_index)
    e = parsed[0]
    assert e.kind == ev.COMPLETED
    assert e.duration_seconds == 12 * 60
    assert e.zone_id == "z1"
    assert e.zone_number == 1


# --- pairing -----------------------------------------------------------------

def _evt(kind, ts, zone_id="z1", device="d1", duration=None, flow=None, number=1):
    return ev.NormalizedEvent(
        source="poll", kind=kind, device_id=device, zone_id=zone_id,
        zone_number=number, zone_name="Front Lawn", timestamp=ts,
        duration_seconds=duration, flow_volume_gallons=flow, schedule_id=None,
        event_id=f"poll:{kind}:{ts.isoformat()}",
    )


def test_pair_started_completed():
    events = [
        _evt(ev.STARTED, _dt(2026, 7, 17, 6, 0), duration=600),
        _evt(ev.COMPLETED, _dt(2026, 7, 17, 6, 10), duration=600, flow=40.0),
    ]
    runs = ev.build_runs(events)
    assert len(runs) == 1
    r = runs[0]
    assert r.complete is True
    assert r.start_time == "2026-07-17T06:00:00+00:00"
    assert r.end_time == "2026-07-17T06:10:00+00:00"
    assert r.duration_seconds == 600
    assert r.flow_volume_gallons == 40.0


def test_completed_without_start_synthesizes_start():
    events = [_evt(ev.COMPLETED, _dt(2026, 7, 17, 6, 10), duration=600)]
    runs = ev.build_runs(events)
    assert len(runs) == 1
    # start synthesized as completion minus duration
    assert runs[0].start_time == "2026-07-17T06:00:00+00:00"
    assert runs[0].complete is True


def test_cycle_soak_marks_run():
    events = [
        _evt(ev.STARTED, _dt(2026, 7, 17, 6, 0), duration=1200),
        _evt(ev.CYCLING, _dt(2026, 7, 17, 6, 5)),
        _evt(ev.COMPLETED, _dt(2026, 7, 17, 6, 20), duration=1200),
    ]
    runs = ev.build_runs(events)
    assert len(runs) == 1
    assert runs[0].was_cycle_soak is True


def test_incomplete_run_when_no_completion():
    events = [_evt(ev.STARTED, _dt(2026, 7, 17, 6, 0), duration=600)]
    runs = ev.build_runs(events)
    assert len(runs) == 1
    assert runs[0].complete is False
    assert runs[0].end_time is None


def test_build_runs_is_deterministic():
    events = [
        _evt(ev.COMPLETED, _dt(2026, 7, 17, 6, 10), duration=600),
        _evt(ev.STARTED, _dt(2026, 7, 17, 6, 0), duration=600),
    ]
    a = ev.build_runs(list(events))
    b = ev.build_runs(list(reversed(events)))
    assert [(r.start_time, r.complete) for r in a] == [(r.start_time, r.complete) for r in b]
