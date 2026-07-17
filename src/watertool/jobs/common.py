"""Shared job helpers: account discovery, event polling, webhook registration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import Settings
from ..db.store import Store
from ..rachio import events as ev
from ..rachio.client import RachioClient, RachioError
from ..rachio.models import Device, devices_from_person
from ..util import to_ms, utcnow

log = logging.getLogger("watertool.jobs")

WEBHOOK_EXTERNAL_ID = "watertool"

# Max coordinate distance (deg) to accept a device<->property GPS match (~1 km).
_PROPERTY_MATCH_TOLERANCE = 0.02


def _format_address(addr: dict) -> str | None:
    parts = [addr.get("lineOne"), addr.get("locality"), addr.get("administrativeArea")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _match_property(device: Device, properties: list[dict]) -> dict | None:
    """Nearest property to the device by GPS, within tolerance."""
    if device.latitude is None or device.longitude is None:
        return None
    best, best_d = None, _PROPERTY_MATCH_TOLERANCE
    for p in properties:
        gp = (p.get("address") or {}).get("geoPoint") or {}
        lat, lon = gp.get("latitude"), gp.get("longitude")
        if lat is None or lon is None:
            continue
        d = ((device.latitude - lat) ** 2 + (device.longitude - lon) ** 2) ** 0.5
        if d <= best_d:
            best, best_d = p, d
    return best


def discover_account(client: RachioClient, store: Store) -> list[str]:
    """Pull the full account tree into the store. Returns device ids.

    Controllers are grouped under their real Rachio property (house), resolved by
    GPS match against the Property service — so multiple controllers at one address
    roll up to one property. If the Property service is unavailable or a device has
    no match, it falls back to a per-controller synthetic property ("dev:<id>").
    """
    person_id = client.get_person_id()
    person = client.get_person(person_id)
    devices = devices_from_person(person)
    properties = client.list_properties(person_id)

    device_ids: list[str] = []
    for d in devices:
        prop = _match_property(d, properties)
        if prop:
            prop_id = prop["id"]
            addr = prop.get("address") or {}
            gp = addr.get("geoPoint") or {}
            store.upsert_property(prop_id, prop.get("name"), _format_address(addr),
                                  gp.get("latitude"), gp.get("longitude"))
        else:
            prop_id = f"dev:{d.id}"
            store.upsert_property(prop_id, d.name, None, d.latitude, d.longitude)
        store.upsert_device(d, property_id=prop_id)
        for z in d.zones:
            store.upsert_zone(z)
        device_ids.append(d.id)
        log.info("discovered %s (%s), %d zones -> property '%s'",
                 d.name, d.id, len(d.zones), prop.get("name") if prop else prop_id)

    store.prune_orphan_properties()
    return device_ids


def poll_device_events(
    client: RachioClient,
    store: Store,
    device_id: str,
    start: datetime,
    end: datetime,
    window_days: int,
) -> int:
    """Fetch events for a device over [start, end], chunked. Store them (deduped).

    Returns the number of newly stored events. Does NOT rebuild runs — callers
    decide the reprocess window.
    """
    zone_index = store.zone_index(device_id)
    new = 0
    cursor = start
    step = timedelta(days=window_days)
    while cursor < end:
        chunk_end = min(cursor + step, end)
        raw = _fetch_events_resilient(client, device_id, cursor, chunk_end)
        for e in ev.parse_poll_events(raw, device_id, zone_index):
            if store.record_event(e, signature_ok=None):  # API-sourced => trusted
                new += 1
        cursor = chunk_end
    return new


def _fetch_events_resilient(
    client: RachioClient, device_id: str, start: datetime, end: datetime,
    min_span: timedelta = timedelta(hours=6),
) -> list[dict]:
    """Fetch events, halving the window on a 400 (Rachio caps the /event range)."""
    try:
        return client.get_device_events(device_id, to_ms(start), to_ms(end))
    except RachioError as exc:
        if exc.status == 400 and (end - start) > min_span:
            mid = start + (end - start) / 2
            return (
                _fetch_events_resilient(client, device_id, start, mid, min_span)
                + _fetch_events_resilient(client, device_id, mid, end, min_span)
            )
        raise


def ensure_webhooks(
    client: RachioClient, store: Store, settings: Settings, device_ids: list[str]
) -> int:
    """Register our webhook on each device if it isn't already. Returns count created.

    Guards against Rachio's silent auto-deregistration (10 consecutive delivery
    failures) — run this from the reconciler so a dropped webhook self-heals.
    """
    if not settings.webhook_url:
        log.warning("PUBLIC_BASE_URL not set; skipping webhook registration")
        return 0

    try:
        types = client.list_webhook_event_types()
    except Exception as exc:  # noqa: BLE001
        log.error("could not list webhook event types: %s", exc)
        return 0
    type_ids = [str(t["id"]) for t in types if "id" in t]

    created = 0
    for device_id in device_ids:
        existing = client.list_device_webhooks(device_id)
        mine = [
            w for w in existing
            if w.get("externalId") == WEBHOOK_EXTERNAL_ID or w.get("url") == settings.webhook_url
        ]
        if mine:
            for w in mine:
                store.record_webhook_registration(
                    w.get("id", ""), device_id, WEBHOOK_EXTERNAL_ID,
                    settings.webhook_url, "legacy", ",".join(type_ids),
                )
            continue
        result = client.create_webhook(
            device_id, settings.webhook_url, type_ids, WEBHOOK_EXTERNAL_ID
        )
        store.record_webhook_registration(
            result.get("id", ""), device_id, WEBHOOK_EXTERNAL_ID,
            settings.webhook_url, "legacy", ",".join(type_ids),
        )
        created += 1
        log.info("registered webhook on device %s", device_id)
    return created


def log_rate_budget(client: RachioClient, store: Store) -> None:
    if client.rate_limit_remaining is not None:
        store.set_poll_state("rate_limit_remaining", str(client.rate_limit_remaining))
        store.set_poll_state("rate_limit_reset", str(client.rate_limit_reset))
        log.info(
            "rachio rate budget: %s/%s remaining (reset %s)",
            client.rate_limit_remaining, client.rate_limit_limit, client.rate_limit_reset,
        )
