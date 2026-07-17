"""watertool command line.

    watertool init-db
    watertool discover              # pull account tree, print a summary
    watertool backfill [--days N]   # one-time history import
    watertool reconcile             # poll + re-register webhooks (run on a schedule)
    watertool register-webhooks     # (re)register webhooks only
    watertool reprocess [--all]     # rebuild runs from stored events
    watertool report [--weeks N]    # gallons/runtime by property/zone/week
    watertool serve [--port 8000]   # run the webhook receiver
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Settings, load_settings
from .db.store import Store
from .rachio.client import RachioClient, RachioError


def _client(settings: Settings) -> RachioClient:
    return RachioClient(
        settings.token,
        base=settings.rachio_api_base,
        cloud_rest_base=settings.rachio_cloud_rest_base,
        min_interval=settings.api_min_interval_seconds,
    )


def _cmd_init_db(settings: Settings, args: argparse.Namespace) -> int:
    Store(settings.database_path).init_db()
    print(f"initialized {settings.database_path}")
    return 0


def _cmd_discover(settings: Settings, args: argparse.Namespace) -> int:
    from .jobs.common import discover_account

    store = Store(settings.database_path)
    store.init_db()
    with _client(settings) as client:
        device_ids = discover_account(client, store)
    for row in store.list_devices():
        standby = " [standby]" if row["on_standby"] else ""
        print(f"  {row['name']}  ({row['id']}){standby}")
    print(f"{len(device_ids)} controller(s).")
    return 0


def _cmd_backfill(settings: Settings, args: argparse.Namespace) -> int:
    from .jobs.backfill import run_backfill

    store = Store(settings.database_path)
    with _client(settings) as client:
        result = run_backfill(client, store, settings, days=args.days)
    print(f"backfilled {result['runs']} runs across {result['devices']} device(s)")
    return 0


def _cmd_reconcile(settings: Settings, args: argparse.Namespace) -> int:
    from .jobs.reconcile import run_reconcile

    store = Store(settings.database_path)
    with _client(settings) as client:
        result = run_reconcile(client, store, settings)
    print(f"reconcile: {result}")
    return 0


def _cmd_register_webhooks(settings: Settings, args: argparse.Namespace) -> int:
    from .jobs.common import discover_account, ensure_webhooks

    if not settings.webhook_url:
        print("error: PUBLIC_BASE_URL is not set", file=sys.stderr)
        return 1
    store = Store(settings.database_path)
    store.init_db()
    with _client(settings) as client:
        device_ids = discover_account(client, store)
        created = ensure_webhooks(client, store, settings, device_ids)
    print(f"registered {created} new webhook(s); target {settings.webhook_url}")
    return 0


def _cmd_reprocess(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.database_path)
    store.init_db()
    lookback = None if args.all else args.days
    total = 0
    for row in store.list_devices():
        total += store.reprocess_device_runs(row["id"], lookback_days=lookback)
    print(f"reprocessed {total} runs")
    return 0


def _cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.database_path)
    store.init_db()
    rows = store.weekly_usage(weeks=args.weeks)
    if not rows:
        print("no runs recorded yet")
        return 0
    print(f"{'week':<10} {'property':<20} {'zone':<20} {'runs':>5} {'min':>7} {'gallons':>9}")
    for r in rows:
        gallons = f"{r['gallons']:.0f}" if r["gallons"] is not None else "-"
        minutes = f"{r['minutes']:.0f}" if r["minutes"] is not None else "-"
        print(f"{r['week']:<10} {(r['property'] or '')[:20]:<20} {(r['zone'] or '')[:20]:<20} "
              f"{r['runs']:>5} {minutes:>7} {gallons:>9}")
    return 0


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from .ingest.receiver import create_app

    store = Store(settings.database_path)
    store.init_db()
    app = create_app(settings, store)
    print(f"serving webhook receiver on {args.host}:{args.port}{settings.webhook_path}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="watertool", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=_cmd_init_db)
    sub.add_parser("discover").set_defaults(func=_cmd_discover)

    bf = sub.add_parser("backfill")
    bf.add_argument("--days", type=int, default=None, help="history window (default from env)")
    bf.set_defaults(func=_cmd_backfill)

    sub.add_parser("reconcile").set_defaults(func=_cmd_reconcile)
    sub.add_parser("register-webhooks").set_defaults(func=_cmd_register_webhooks)

    rp = sub.add_parser("reprocess")
    rp.add_argument("--days", type=int, default=90, help="lookback window (default 90)")
    rp.add_argument("--all", action="store_true", help="rebuild full history")
    rp.set_defaults(func=_cmd_reprocess)

    rep = sub.add_parser("report")
    rep.add_argument("--weeks", type=int, default=8)
    rep.set_defaults(func=_cmd_report)

    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=_cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    try:
        return args.func(settings, args)
    except RachioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.body:
            print(exc.body, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
