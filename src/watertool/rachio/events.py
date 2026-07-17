"""Normalize Rachio events and pair them into zone runs.

Rachio emits watering events in three different shapes, and watertool has to
treat them uniformly:

  1. webhook_v2  — new cloud-rest WebhookService: {eventId, eventType, resourceId,
                   timestamp, payload:{...}}. eventType like
                   DEVICE_ZONE_RUN_COMPLETED_EVENT.
  2. webhook_legacy — old /public/notification webhooks: {type/subType, deviceId,
                   zoneId, zoneNumber, zoneName, duration, durationInMinutes,
                   flowVolume, timestamp}.
  3. poll        — GET /public/device/{id}/event: thin. Duration/zone are often
                   only in the English `summary` string and must be parsed out.

Every shape collapses to a NormalizedEvent. build_runs() then walks a
chronological event stream and pairs ZONE_STARTED -> ZONE_COMPLETED/STOPPED into
Run records. build_runs is pure (no DB, no clock) so it is fully unit-testable and
deterministic — the same events always yield the same runs, which is what makes
re-processing idempotent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from ..util import from_ms, to_iso

# --- kinds we care about -----------------------------------------------------
STARTED = "ZONE_STARTED"
COMPLETED = "ZONE_COMPLETED"
STOPPED = "ZONE_STOPPED"
CYCLING = "ZONE_CYCLING"
CYCLING_COMPLETED = "ZONE_CYCLING_COMPLETED"
PAUSED = "ZONE_PAUSED"

# Map assorted provider spellings onto our canonical zone-event kinds.
_V2_KIND = {
    "DEVICE_ZONE_RUN_STARTED_EVENT": STARTED,
    "DEVICE_ZONE_RUN_COMPLETED_EVENT": COMPLETED,
    "DEVICE_ZONE_RUN_STOPPED_EVENT": STOPPED,
    "DEVICE_ZONE_RUN_PAUSED_EVENT": PAUSED,
}
_ZONE_KINDS = {STARTED, COMPLETED, STOPPED, CYCLING, CYCLING_COMPLETED, PAUSED}

_DURATION_RE = re.compile(r"for\s+(\d+)\s*(second|minute|hour)s?", re.IGNORECASE)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}


@dataclass
class NormalizedEvent:
    source: str  # webhook_v2 | webhook_legacy | poll
    kind: str  # canonical kind (STARTED/COMPLETED/...), or raw type for non-zone
    device_id: str | None
    zone_id: str | None
    zone_number: int | None
    zone_name: str | None
    timestamp: datetime | None
    duration_seconds: int | None
    flow_volume_gallons: float | None
    schedule_id: str | None
    event_id: str
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_zone_event(self) -> bool:
        return self.kind in _ZONE_KINDS

    @property
    def ts_iso(self) -> str | None:
        return to_iso(self.timestamp)


@dataclass
class Run:
    device_id: str | None
    zone_id: str | None
    zone_number: int | None
    zone_name: str | None
    schedule_id: str | None
    source: str
    start_time: str  # ISO-8601 UTC
    end_time: str | None
    duration_seconds: int | None
    flow_volume_gallons: float | None
    was_cycle_soak: bool
    complete: bool
    event_id_start: str | None
    event_id_complete: str | None


# --- helpers -----------------------------------------------------------------

def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(d: dict) -> datetime | None:
    """Accept epoch-ms ints or ISO strings under any of Rachio's timestamp keys."""
    for key in ("timestamp", "eventDate", "createDate", "eventTime", "time"):
        if key not in d or d[key] in (None, ""):
            continue
        v = d[key]
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return from_ms(v)
        try:
            s = str(v).replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        except ValueError:
            continue
    return None


def _duration_from_summary(summary: str | None) -> int | None:
    if not summary:
        return None
    m = _DURATION_RE.search(summary)
    if not m:
        return None
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


def _stable_id(source: str, provider_id: Any, parts: Iterable[Any]) -> str:
    if provider_id:
        return f"{source}:{provider_id}"
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]
    return f"{source}:h:{digest}"


# --- parsers -----------------------------------------------------------------

def parse_webhook(body: dict) -> tuple[list[NormalizedEvent], str]:
    """Parse one webhook POST body. Returns (events, provider)."""
    # v2 WebhookService bodies carry eventId + resourceId + a nested payload.
    if "eventId" in body or ("resourceId" in body and "payload" in body):
        return _parse_v2(body), "webhook_v2"
    return _parse_legacy(body), "webhook_legacy"


def _parse_v2(body: dict) -> list[NormalizedEvent]:
    payload = body.get("payload") or {}
    etype = body.get("eventType", "")
    kind = _V2_KIND.get(etype, etype)
    device_id = body.get("resourceId") or payload.get("deviceId")
    ev = NormalizedEvent(
        source="webhook_v2",
        kind=kind,
        device_id=device_id,
        zone_id=payload.get("zoneId") or payload.get("id"),
        zone_number=_int(payload.get("zoneNumber")),
        zone_name=payload.get("zoneName") or payload.get("name"),
        timestamp=_parse_ts(body) or _parse_ts(payload),
        duration_seconds=(
            _int(payload.get("durationSeconds"))
            or _int(payload.get("duration"))
            or (_int(payload.get("durationInMinutes")) or 0) * 60 or None
        ),
        flow_volume_gallons=_float(
            payload.get("flowVolume") or payload.get("flowVolumeG")
        ),
        schedule_id=payload.get("scheduleId") or payload.get("scheduleRuleId"),
        event_id=_stable_id("webhook_v2", body.get("eventId"),
                            [device_id, etype, body.get("timestamp")]),
        raw=body,
    )
    return [ev]


def _parse_legacy(body: dict) -> list[NormalizedEvent]:
    sub = body.get("subType") or body.get("type") or ""
    kind = sub if sub in _ZONE_KINDS else sub
    duration = (
        _int(body.get("duration"))
        or (_int(body.get("durationInMinutes")) or 0) * 60 or None
        or _duration_from_summary(body.get("summary"))
    )
    device_id = body.get("deviceId") or (body.get("device") or {}).get("id")
    ev = NormalizedEvent(
        source="webhook_legacy",
        kind=kind,
        device_id=device_id,
        zone_id=body.get("zoneId"),
        zone_number=_int(body.get("zoneNumber")),
        zone_name=body.get("zoneName"),
        timestamp=_parse_ts(body),
        duration_seconds=duration,
        flow_volume_gallons=_float(body.get("flowVolume")),
        schedule_id=body.get("scheduleId") or body.get("scheduleRuleId"),
        event_id=_stable_id("webhook_legacy", body.get("id"),
                            [device_id, body.get("zoneId"), sub, body.get("timestamp")]),
        raw=body,
    )
    return [ev]


def parse_poll_events(
    events: list[dict],
    device_id: str,
    zone_index: dict[str, dict] | None = None,
) -> list[NormalizedEvent]:
    """Parse a `GET /device/{id}/event` array.

    zone_index maps a zone NAME -> {"id", "zoneNumber"} so we can recover the zone
    id from the summary text on the (many) poll events that omit zoneId.
    """
    out: list[NormalizedEvent] = []
    for d in events:
        sub = d.get("subType") or d.get("type") or ""
        if sub not in _ZONE_KINDS:
            # keep non-zone events too (schedule/device) so raw storage is complete
            out.append(_normalize_nonzone_poll(d, device_id))
            continue
        zone_id = d.get("zoneId")
        zone_name = d.get("zoneName")
        zone_number = _int(d.get("zoneNumber"))
        summary = d.get("summary")
        if not zone_id and zone_index and summary:
            zone_id, zone_number = _resolve_zone_from_summary(
                summary, zone_index, zone_name
            )
        duration = (
            _int(d.get("duration"))
            or (_int(d.get("durationInMinutes")) or 0) * 60 or None
            or _duration_from_summary(summary)
        )
        out.append(
            NormalizedEvent(
                source="poll",
                kind=sub,
                device_id=device_id,
                zone_id=zone_id,
                zone_number=zone_number,
                zone_name=zone_name,
                timestamp=_parse_ts(d),
                duration_seconds=duration,
                flow_volume_gallons=_float(d.get("flowVolume")),
                schedule_id=d.get("scheduleId"),
                event_id=_stable_id("poll", d.get("id"),
                                    [device_id, zone_id, sub, d.get("eventDate")]),
                raw=d,
            )
        )
    return out


def _normalize_nonzone_poll(d: dict, device_id: str) -> NormalizedEvent:
    sub = d.get("subType") or d.get("type") or "UNKNOWN"
    return NormalizedEvent(
        source="poll",
        kind=sub,
        device_id=device_id,
        zone_id=None,
        zone_number=None,
        zone_name=None,
        timestamp=_parse_ts(d),
        duration_seconds=None,
        flow_volume_gallons=None,
        schedule_id=d.get("scheduleId"),
        event_id=_stable_id("poll", d.get("id"),
                            [device_id, sub, d.get("eventDate"), d.get("summary")]),
        raw=d,
    )


def _resolve_zone_from_summary(
    summary: str, zone_index: dict[str, dict], zone_name: str | None
) -> tuple[str | None, int | None]:
    # Prefer an explicit zoneName; otherwise match any known zone name that appears
    # in the summary (longest name first, so "Front Lawn" wins over "Lawn").
    if zone_name and zone_name in zone_index:
        z = zone_index[zone_name]
        return z.get("id"), z.get("zoneNumber")
    for name in sorted(zone_index, key=len, reverse=True):
        if name and name in summary:
            z = zone_index[name]
            return z.get("id"), z.get("zoneNumber")
    return None, None


# --- pairing -----------------------------------------------------------------

def build_runs(events: Iterable[NormalizedEvent]) -> list[Run]:
    """Pair a chronological event stream into zone runs.

    Deterministic and idempotent: feed it the full event history for a device and
    it reconstructs every run. A COMPLETED/STOPPED with no preceding STARTED (a
    lost start event) is still recorded, with start_time synthesized from the
    completion minus its reported duration.
    """
    zone_events = [e for e in events if e.is_zone_event and e.timestamp is not None]
    zone_events.sort(key=lambda e: e.timestamp)  # type: ignore[arg-type,return-value]

    open_runs: dict[Any, Run] = {}
    runs: list[Run] = []

    def key(e: NormalizedEvent) -> Any:
        return e.zone_id or (e.device_id, e.zone_number)

    for e in zone_events:
        k = key(e)
        if e.kind == STARTED:
            # A new start supersedes any dangling open run for the same zone.
            if k in open_runs:
                runs.append(open_runs.pop(k))
            open_runs[k] = Run(
                device_id=e.device_id, zone_id=e.zone_id, zone_number=e.zone_number,
                zone_name=e.zone_name, schedule_id=e.schedule_id, source=e.source,
                start_time=e.ts_iso, end_time=None,
                duration_seconds=e.duration_seconds, flow_volume_gallons=None,
                was_cycle_soak=False, complete=False,
                event_id_start=e.event_id, event_id_complete=None,
            )
        elif e.kind in (COMPLETED, STOPPED):
            run = open_runs.pop(k, None)
            if run is None:
                # Lost start: synthesize the run envelope from the completion.
                start_iso = e.ts_iso
                if e.duration_seconds and e.timestamp is not None:
                    from datetime import timedelta

                    start_iso = to_iso(e.timestamp - timedelta(seconds=e.duration_seconds))
                run = Run(
                    device_id=e.device_id, zone_id=e.zone_id, zone_number=e.zone_number,
                    zone_name=e.zone_name, schedule_id=e.schedule_id, source=e.source,
                    start_time=start_iso, end_time=None, duration_seconds=e.duration_seconds,
                    flow_volume_gallons=None, was_cycle_soak=False, complete=False,
                    event_id_start=None, event_id_complete=None,
                )
            run.end_time = e.ts_iso
            run.complete = e.kind == COMPLETED
            run.event_id_complete = e.event_id
            if e.flow_volume_gallons is not None:
                run.flow_volume_gallons = e.flow_volume_gallons
            if e.duration_seconds:
                run.duration_seconds = e.duration_seconds
            elif run.duration_seconds is None and run.start_time and run.end_time:
                run.duration_seconds = _elapsed_seconds(run.start_time, run.end_time)
            # carry the best zone identity we have
            run.zone_name = run.zone_name or e.zone_name
            run.zone_number = run.zone_number if run.zone_number is not None else e.zone_number
            runs.append(run)
        elif e.kind in (CYCLING, CYCLING_COMPLETED):
            if k in open_runs:
                open_runs[k].was_cycle_soak = True
        # PAUSED: leave the run open; a later COMPLETED/STOPPED closes it.

    # Emit runs that started but never completed within this window.
    runs.extend(open_runs.values())
    runs.sort(key=lambda r: r.start_time)
    return runs


def _elapsed_seconds(start_iso: str, end_iso: str) -> int | None:
    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        return max(0, int((e - s).total_seconds()))
    except ValueError:
        return None
