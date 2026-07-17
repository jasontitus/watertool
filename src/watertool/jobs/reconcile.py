"""Reconciler — the safety net under the webhooks.

Webhooks are treated as lossy (Rachio can drop deliveries, or auto-deregister a
webhook after 10 failures). This job, run on a schedule (e.g. hourly), refreshes
the account tree, re-registers any missing webhook, and polls each device's events
over an overlapping window so anything the webhooks missed still lands. Cheap: a
handful of calls against the 3,500/day budget.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..db.store import Store
from ..rachio.client import RachioClient
from ..util import days_ago, utcnow
from .common import discover_account, ensure_webhooks, log_rate_budget, poll_device_events

log = logging.getLogger("watertool.reconcile")


def run_reconcile(client: RachioClient, store: Store, settings: Settings) -> dict:
    store.init_db()
    device_ids = discover_account(client, store)

    created = ensure_webhooks(client, store, settings, device_ids)

    overlap = settings.reconcile_overlap_days
    start, end = days_ago(overlap), utcnow()
    total_new = 0
    total_runs = 0
    for device_id in device_ids:
        new = poll_device_events(
            client, store, device_id, start, end, settings.event_window_days
        )
        runs = store.reprocess_device_runs(device_id, lookback_days=overlap + 2)
        total_new += new
        total_runs += runs

    log_rate_budget(client, store)
    store.set_poll_state("last_reconcile", utcnow().isoformat())
    log.info(
        "reconcile: %d devices, %d webhooks created, %d new events, %d runs touched",
        len(device_ids), created, total_new, total_runs,
    )
    return {"devices": len(device_ids), "webhooks_created": created, "new_events": total_new}
