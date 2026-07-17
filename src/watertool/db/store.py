"""SQLite persistence layer.

Design notes:
  * Short-lived connections per operation (WAL mode) — safe under the receiver's
    threadpool and the CLI jobs without a connection-sharing dance. At 4
    controllers the write volume is trivial.
  * The store is the ONE place that turns events into runs. record_event() appends
    to the events_raw ledger (deduped); reprocess_device_runs() rebuilds zone_runs
    for a device from that ledger via events.build_runs(). Webhook ingest, poll
    reconcile, and backfill all funnel through this same path, so runs are always a
    pure, idempotent function of the stored events.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from ..rachio import events as ev
from ..rachio.gallons import estimate_gallons
from ..rachio.models import Device, Zone
from ..util import to_iso, utcnow

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # For :memory: we must keep a single shared connection alive.
        self._mem_conn: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._mem_conn = self._new_conn()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            yield self._mem_conn
            self._mem_conn.commit()
            return
        conn = self._new_conn()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # --- account tree ------------------------------------------------------

    def upsert_property(
        self, prop_id: str, name: str | None, address: str | None,
        latitude: float | None, longitude: float | None,
    ) -> None:
        now = to_iso(utcnow())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO properties (id, name, address, latitude, longitude, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, address=excluded.address,
                    latitude=excluded.latitude, longitude=excluded.longitude,
                    updated_at=excluded.updated_at
                """,
                (prop_id, name, address, latitude, longitude, now, now),
            )

    def upsert_device(self, device: Device, property_id: str | None) -> None:
        now = to_iso(utcnow())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO devices (id, property_id, name, model, serial_number, mac_address,
                    latitude, longitude, timezone, status, on_standby, raw_json, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    property_id=excluded.property_id, name=excluded.name, model=excluded.model,
                    serial_number=excluded.serial_number, mac_address=excluded.mac_address,
                    latitude=excluded.latitude, longitude=excluded.longitude,
                    timezone=excluded.timezone, status=excluded.status,
                    on_standby=excluded.on_standby, raw_json=excluded.raw_json,
                    last_seen=excluded.last_seen
                """,
                (device.id, property_id, device.name, device.model, device.serial_number,
                 device.mac_address, device.latitude, device.longitude, device.timezone,
                 device.status, int(device.on_standby), json.dumps(device.raw), now, now),
            )

    def upsert_zone(self, zone: Zone) -> None:
        now = to_iso(utcnow())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO zones (id, device_id, zone_number, name, enabled, area_sqft,
                    inches_per_hour, efficiency, soil, crop, nozzle_head, image_url, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    device_id=excluded.device_id, zone_number=excluded.zone_number,
                    name=excluded.name, enabled=excluded.enabled, area_sqft=excluded.area_sqft,
                    inches_per_hour=excluded.inches_per_hour, efficiency=excluded.efficiency,
                    soil=excluded.soil, crop=excluded.crop, nozzle_head=excluded.nozzle_head,
                    image_url=excluded.image_url, raw_json=excluded.raw_json, updated_at=excluded.updated_at
                """,
                (zone.id, zone.device_id, zone.zone_number, zone.name,
                 None if zone.enabled is None else int(zone.enabled), zone.area_sqft,
                 zone.inches_per_hour, zone.efficiency, zone.soil, zone.crop,
                 zone.nozzle_head, zone.image_url, json.dumps(zone.raw), now),
            )

    def zone_index(self, device_id: str) -> dict[str, dict]:
        """name -> {id, zoneNumber} for resolving zones from poll summaries."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, zone_number FROM zones WHERE device_id = ?",
                (device_id,),
            ).fetchall()
        return {r["name"]: {"id": r["id"], "zoneNumber": r["zone_number"]} for r in rows if r["name"]}

    def list_devices(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM devices ORDER BY name").fetchall()

    def prune_orphan_properties(self) -> int:
        """Delete property rows no device points at (e.g. superseded synthetic ones)."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM properties WHERE id NOT IN "
                "(SELECT property_id FROM devices WHERE property_id IS NOT NULL)"
            )
            return cur.rowcount

    # --- events + runs -----------------------------------------------------

    def record_event(
        self, e: ev.NormalizedEvent, signature_ok: bool | None
    ) -> bool:
        """Append one event to the ledger. Returns True if newly inserted."""
        now = to_iso(utcnow())
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO events_raw
                    (dedup_key, source, kind, device_id, zone_id, zone_number, zone_name,
                     duration_seconds, flow_volume_gallons, schedule_id, timestamp,
                     received_at, signature_ok, body_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (e.event_id, e.source, e.kind, e.device_id, e.zone_id, e.zone_number,
                 e.zone_name, e.duration_seconds, e.flow_volume_gallons, e.schedule_id,
                 e.ts_iso, now, None if signature_ok is None else int(signature_ok),
                 json.dumps(e.raw)),
            )
            return cur.rowcount > 0

    def _load_events(self, device_id: str, since_iso: str | None) -> list[ev.NormalizedEvent]:
        from datetime import datetime

        # Trust API-sourced events (signature_ok IS NULL for poll/backfill) and
        # signature-verified webhooks; never let a failed-signature webhook (0)
        # influence runs, though it stays in the ledger for forensics.
        sql = "SELECT * FROM events_raw WHERE device_id = ? AND (signature_ok IS NULL OR signature_ok = 1)"
        params: list[Any] = [device_id]
        if since_iso:
            sql += " AND timestamp >= ?"
            params.append(since_iso)
        sql += " ORDER BY timestamp"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()

        out: list[ev.NormalizedEvent] = []
        for r in rows:
            ts = None
            if r["timestamp"]:
                try:
                    ts = datetime.fromisoformat(r["timestamp"])
                except ValueError:
                    ts = None
            out.append(
                ev.NormalizedEvent(
                    source=r["source"], kind=r["kind"], device_id=r["device_id"],
                    zone_id=r["zone_id"], zone_number=r["zone_number"],
                    zone_name=r["zone_name"], timestamp=ts,
                    duration_seconds=r["duration_seconds"],
                    flow_volume_gallons=r["flow_volume_gallons"],
                    schedule_id=r["schedule_id"], event_id=r["dedup_key"], raw={},
                )
            )
        return out

    def reprocess_device_runs(
        self, device_id: str, lookback_days: float | None = 90
    ) -> int:
        """Rebuild zone_runs for a device from its stored events. Returns run count.

        lookback_days bounds the work (None = full history). We widen the window a
        little past the cutoff so a run that started just before it still pairs.
        """
        since_iso = None
        if lookback_days is not None:
            since_iso = to_iso(utcnow() - timedelta(days=lookback_days))
        events = self._load_events(device_id, since_iso)
        runs = ev.build_runs(events)

        zones = self._zone_config_map(device_id)
        count = 0
        for r in runs:
            gallons = self._gallons_for_run(r, zones)
            self._upsert_run(r, gallons)
            count += 1
        return count

    def _zone_config_map(self, device_id: str) -> dict[str, sqlite3.Row]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM zones WHERE device_id = ?", (device_id,)
            ).fetchall()
        by_id = {r["id"]: r for r in rows}
        by_number = {r["zone_number"]: r for r in rows}
        return {"by_id": by_id, "by_number": by_number}  # type: ignore[return-value]

    def _gallons_for_run(self, r: ev.Run, zones: dict) -> float | None:
        z = None
        if r.zone_id and r.zone_id in zones["by_id"]:
            z = zones["by_id"][r.zone_id]
        elif r.zone_number is not None and r.zone_number in zones["by_number"]:
            z = zones["by_number"][r.zone_number]
        if z is None:
            return None
        return estimate_gallons(z["inches_per_hour"], z["area_sqft"], r.duration_seconds)

    def _upsert_run(self, r: ev.Run, gallons: float | None) -> None:
        now = to_iso(utcnow())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO zone_runs
                    (device_id, zone_id, zone_number, zone_name, schedule_id, source,
                     start_time, end_time, duration_seconds, gallons_estimated,
                     flow_volume_gallons, was_cycle_soak, complete, event_id_start,
                     event_id_complete, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(zone_id, start_time) DO UPDATE SET
                    end_time=excluded.end_time, duration_seconds=excluded.duration_seconds,
                    gallons_estimated=excluded.gallons_estimated,
                    flow_volume_gallons=COALESCE(excluded.flow_volume_gallons, zone_runs.flow_volume_gallons),
                    was_cycle_soak=excluded.was_cycle_soak, complete=excluded.complete,
                    zone_name=COALESCE(excluded.zone_name, zone_runs.zone_name),
                    zone_number=COALESCE(excluded.zone_number, zone_runs.zone_number),
                    schedule_id=COALESCE(excluded.schedule_id, zone_runs.schedule_id),
                    event_id_complete=COALESCE(excluded.event_id_complete, zone_runs.event_id_complete),
                    updated_at=excluded.updated_at
                """,
                (r.device_id, r.zone_id, r.zone_number, r.zone_name, r.schedule_id,
                 r.source, r.start_time, r.end_time, r.duration_seconds, gallons,
                 r.flow_volume_gallons, int(r.was_cycle_soak), int(r.complete),
                 r.event_id_start, r.event_id_complete, now, now),
            )

    # --- poll bookkeeping --------------------------------------------------

    def get_poll_state(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM poll_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_poll_state(self, key: str, value: str) -> None:
        now = to_iso(utcnow())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO poll_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def record_webhook_registration(
        self, webhook_id: str, device_id: str, external_id: str, url: str,
        provider: str, event_types: str,
    ) -> None:
        now = to_iso(utcnow())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO webhook_registrations
                    (id, device_id, external_id, url, provider, event_types, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    device_id=excluded.device_id, external_id=excluded.external_id,
                    url=excluded.url, provider=excluded.provider,
                    event_types=excluded.event_types, last_seen=excluded.last_seen
                """,
                (webhook_id, device_id, external_id, url, provider, event_types, now, now),
            )

    # --- reporting ---------------------------------------------------------

    # --- dashboard API ------------------------------------------------------

    def property_overview(self) -> list[dict]:
        """Per-property rollup for the dashboard: status, totals, monthly sparkline."""
        out: list[dict] = []
        with self._conn() as c:
            props = c.execute("SELECT * FROM properties ORDER BY name").fetchall()
            for p in props:
                devs = c.execute(
                    "SELECT id, name, status, model FROM devices WHERE property_id=? ORDER BY name",
                    (p["id"],),
                ).fetchall()
                dev_ids = [d["id"] for d in devs]
                ph = ",".join("?" * len(dev_ids)) or "NULL"
                zones = c.execute(
                    f"SELECT COUNT(*) n FROM zones WHERE device_id IN ({ph})", dev_ids
                ).fetchone()["n"] if dev_ids else 0
                agg = c.execute(
                    f"""SELECT COUNT(*) runs, MIN(start_time) first, MAX(start_time) last,
                        SUM(COALESCE(flow_volume_gallons, gallons_estimated)) gallons,
                        SUM(duration_seconds)/3600.0 hours
                        FROM zone_runs WHERE device_id IN ({ph})""",
                    dev_ids,
                ).fetchone() if dev_ids else None
                monthly = c.execute(
                    f"""SELECT substr(start_time,1,7) month,
                        SUM(COALESCE(flow_volume_gallons, gallons_estimated)) gallons
                        FROM zone_runs WHERE device_id IN ({ph})
                        GROUP BY month ORDER BY month""",
                    dev_ids,
                ).fetchall() if dev_ids else []
                out.append({
                    "id": p["id"],
                    "name": p["name"],
                    "address": p["address"],
                    "status": "ONLINE" if any(d["status"] == "ONLINE" for d in devs) else "OFFLINE",
                    "controllers": [
                        {"name": d["name"], "status": d["status"], "model": d["model"]}
                        for d in devs
                    ],
                    "zones": zones,
                    "runs": agg["runs"] if agg else 0,
                    "gallons": round(agg["gallons"]) if agg and agg["gallons"] else 0,
                    "hours": round(agg["hours"], 1) if agg and agg["hours"] else 0,
                    "first_run": agg["first"] if agg else None,
                    "last_run": agg["last"] if agg else None,
                    "monthly": [{"month": m["month"], "gallons": round(m["gallons"] or 0)} for m in monthly],
                })
        return out

    def monthly_by_property(self, months: int = 12) -> list[dict]:
        since = to_iso(utcnow() - timedelta(days=months * 31))
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT p.id pid, p.name property, substr(r.start_time,1,7) month,
                       COUNT(*) runs,
                       SUM(COALESCE(r.flow_volume_gallons, r.gallons_estimated)) gallons
                FROM zone_runs r
                JOIN devices d ON d.id = r.device_id
                JOIN properties p ON p.id = d.property_id
                WHERE r.start_time >= ?
                GROUP BY pid, month ORDER BY month
                """,
                (since,),
            ).fetchall()
        return [{"pid": r["pid"], "property": r["property"], "month": r["month"],
                 "runs": r["runs"], "gallons": round(r["gallons"] or 0)} for r in rows]

    def zone_usage(self, property_id: str | None = None, limit: int = 40) -> list[dict]:
        sql = """
            SELECT p.id pid, p.name property,
                   COALESCE(z.name, 'zone ' || r.zone_number, '(unattributed)') zone,
                   COUNT(*) runs,
                   SUM(r.gallons_estimated) gallons,
                   SUM(r.duration_seconds)/60.0 minutes,
                   z.inches_per_hour iph, z.area_sqft area, z.efficiency eff
            FROM zone_runs r
            JOIN devices d ON d.id = r.device_id
            JOIN properties p ON p.id = d.property_id
            LEFT JOIN zones z ON z.id = r.zone_id
        """
        params: list[Any] = []
        if property_id:
            sql += " WHERE p.id = ?"
            params.append(property_id)
        sql += " GROUP BY pid, zone ORDER BY gallons DESC NULLS LAST LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [{"pid": r["pid"], "property": r["property"], "zone": r["zone"],
                 "runs": r["runs"], "gallons": round(r["gallons"]) if r["gallons"] else 0,
                 "minutes": round(r["minutes"]) if r["minutes"] else 0,
                 "iph": r["iph"], "area": r["area"], "eff": r["eff"]} for r in rows]

    def recent_runs(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT p.name property,
                       COALESCE(z.name, 'zone ' || r.zone_number, '(unattributed)') zone,
                       r.start_time, r.duration_seconds,
                       COALESCE(r.flow_volume_gallons, r.gallons_estimated) gallons,
                       r.complete, r.was_cycle_soak
                FROM zone_runs r
                JOIN devices d ON d.id = r.device_id
                JOIN properties p ON p.id = d.property_id
                LEFT JOIN zones z ON z.id = r.zone_id
                ORDER BY r.start_time DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{"property": r["property"], "zone": r["zone"], "start": r["start_time"],
                 "duration_min": round((r["duration_seconds"] or 0) / 60, 1),
                 "gallons": round(r["gallons"]) if r["gallons"] else None,
                 "complete": bool(r["complete"]), "cycle_soak": bool(r["was_cycle_soak"])}
                for r in rows]

    def weekly_usage(self, weeks: int = 8) -> list[sqlite3.Row]:
        """Gallons + runtime per property/zone/ISO-week for the last N weeks."""
        since = to_iso(utcnow() - timedelta(weeks=weeks))
        with self._conn() as c:
            return c.execute(
                """
                SELECT
                    COALESCE(p.name, d.name, r.device_id)      AS property,
                    -- polled events omit zone_name; resolve it from the zones table
                    COALESCE(z.name, r.zone_name, 'zone ' || r.zone_number, '(unattributed)') AS zone,
                    strftime('%Y-W%W', r.start_time)           AS week,
                    COUNT(*)                                    AS runs,
                    SUM(r.duration_seconds) / 60.0             AS minutes,
                    SUM(COALESCE(r.flow_volume_gallons, r.gallons_estimated)) AS gallons
                FROM zone_runs r
                LEFT JOIN devices d ON d.id = r.device_id
                LEFT JOIN properties p ON p.id = d.property_id
                LEFT JOIN zones z ON z.id = r.zone_id
                WHERE r.start_time >= ?
                GROUP BY property, zone, week
                ORDER BY week DESC, property, zone
                """,
                (since,),
            ).fetchall()
