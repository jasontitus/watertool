"""Day-one backfill.

Rachio retains only ~12 months of event history, so this grabs everything
available now and turns it into runs before it ages out. Run once at setup; the
reconciler keeps it current after that.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..db.store import Store
from ..rachio.client import RachioClient
from ..util import days_ago, utcnow
from .common import discover_account, log_rate_budget, poll_device_events

log = logging.getLogger("watertool.backfill")


def run_backfill(
    client: RachioClient, store: Store, settings: Settings, days: int | None = None
) -> dict:
    days = days or settings.backfill_days
    store.init_db()
    device_ids = discover_account(client, store)

    start, end = days_ago(days), utcnow()
    summary: dict[str, int] = {}
    for device_id in device_ids:
        new = poll_device_events(
            client, store, device_id, start, end, settings.event_window_days
        )
        # Rebuild the full history for this device from everything we just stored.
        runs = store.reprocess_device_runs(device_id, lookback_days=None)
        summary[device_id] = runs
        log.info("device %s: %d new events, %d runs", device_id, new, runs)

    log_rate_budget(client, store)
    store.set_poll_state("last_backfill", utcnow().isoformat())
    total_runs = sum(summary.values())
    log.info("backfill complete: %d devices, %d runs", len(device_ids), total_runs)
    return {"devices": len(device_ids), "runs": total_runs, "per_device": summary}
