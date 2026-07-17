# watertool

Track and optimize irrigation across multiple [Rachio](https://rachio.com)-equipped
properties. Built because the Rachio app has no cross-property view and its public
API keeps **no durable usage history** — so watertool captures every watering event
itself and becomes the system of record.

See [RESEARCH.md](./RESEARCH.md) for the full API / ecosystem / sensor research this
is based on.

## What it does today

- **Ingests** every zone run across all your controllers via Rachio webhooks (real
  time) with a polling reconciler as a safety net.
- **Stores** runs in SQLite as the durable record Rachio's API doesn't provide
  (Rachio keeps only ~12 months of events; watertool keeps them forever).
- **Estimates gallons** per run from each zone's nozzle precip-rate and area (the
  same math Rachio's own usage screen uses), and records metered gallons if a flow
  meter is attached.
- **Reports** gallons and runtime by property / zone / week.
- Exposes Rachio's official control levers (manual runs, seasonal adjustment,
  schedule skip, rain delay, and Flex Daily `setMoisturePercent`) for future
  sensor-driven automation.

## Architecture

```
Rachio controllers ──webhooks──▶ receiver (FastAPI) ─┐
                   ──poll──────▶ reconciler ─────────┼─▶ events_raw ─▶ zone_runs ─▶ report / Grafana
                                                      │      (ledger)    (paired)
external sensors (Ecowitt/YoLink) ──▶ sensor_readings┘   [phase 2]
Flume / submeter ─────────────────▶ flow_readings        [phase 2]
```

Every ingestion path (webhook, poll, backfill) writes to one append-only
`events_raw` ledger, then `zone_runs` is rebuilt from that ledger by a pure,
deterministic pairing function — so re-processing is always idempotent and lossy
webhooks self-heal on the next reconcile.

## Setup

Requires Python 3.11+ (managed automatically by [uv](https://docs.astral.sh/uv/)).

```bash
uv sync                     # create venv, install deps
cp .env.example .env        # then edit .env
uv run pytest               # 36 tests, no network needed
```

### 1. Consolidate controllers onto one Rachio account

The API can only see controllers on the **token's own account** — guest/shared
("Complete Access") controllers are invisible. Move all 4 controllers under one
account first. One account = one token = one shared 3,500-calls/day budget (plenty;
watertool is webhook-first and polls sparingly).

### 2. Get your API token

Rachio app → Account Settings → **Get API Key**. Put it in `.env` as
`RACHIO_API_KEY` (or `RACHIO_API_TOKEN` — both work). It's a static bearer token;
keep it out of git (`.env` is gitignored) and out of logs (the code treats it as a
secret).

### 3. Initialize and backfill

```bash
uv run watertool init-db
uv run watertool discover      # sanity-check: lists your controllers
uv run watertool backfill      # pull ~12 months of history before it ages out
uv run watertool report        # see it
```

### 4. Stand up the webhook receiver (real-time tracking)

The receiver needs a **public HTTPS URL** Rachio can POST to. Set `PUBLIC_BASE_URL`
in `.env`, then:

```bash
uv run watertool serve --port 8000     # behind a reverse proxy / tunnel
uv run watertool register-webhooks     # point Rachio at PUBLIC_BASE_URL
```

Easiest ways to get a public URL: a small VPS with Caddy/Traefik for TLS, Fly.io /
Render / Cloud Run, or a Cloudflare/Tailscale Funnel tunnel to a home box.

### 5. Keep it current

Run the reconciler on a schedule (cron/systemd timer/launchd), e.g. hourly:

```bash
uv run watertool reconcile
```

It refreshes the account tree, **re-registers any webhook Rachio silently dropped**
(Rachio auto-removes a webhook after 10 consecutive delivery failures), and polls an
overlapping window so nothing is lost.

## Commands

| Command | What |
|---|---|
| `watertool init-db` | Create the SQLite schema |
| `watertool discover` | Pull the account tree, list controllers |
| `watertool backfill [--days N]` | One-time history import (default 365 days) |
| `watertool reconcile` | Poll + re-register webhooks (run on a schedule) |
| `watertool register-webhooks` | (Re)register webhooks only |
| `watertool reprocess [--all]` | Rebuild runs from stored events |
| `watertool report [--weeks N]` | Gallons/runtime by property/zone/week |
| `watertool serve [--port N]` | Run the webhook receiver |

## Validate against your own account

A few things in the research couldn't be pinned down without a live token — the code
is defensive about them, but confirm on your setup:

- **Webhook signature scheme.** The new Rachio WebhookService signs with an
  `x-signature` HMAC, but its exact canonicalization isn't fully documented.
  `webhooks.py` verifies against the raw body (hex/base64). The receiver stores every
  request regardless and only builds runs from verified events, so if verification
  fails at first, no data is lost — set `WEBHOOK_VERIFY=none` briefly, capture a real
  delivery, confirm the scheme, then switch back. (If you use legacy webhooks with
  basic-auth-in-URL instead, set `WEBHOOK_VERIFY=basic`.)
- **Poll event shape.** `GET /device/{id}/event` payloads are thin; zone/duration are
  parsed from explicit fields when present and from the summary string otherwise.
- **Gallons accuracy.** Estimates are only as good as each zone's nozzle precip-rate
  and area in the Rachio app. Calibrate per zone (catch-cup test) or add a Flume for
  ground truth.

## Roadmap

- **Phase 2 — sensors.** Land Ecowitt/YoLink soil moisture into `sensor_readings`
  (an Ecowitt local-push endpoint is stubbed at `POST /webhooks/ecowitt`) and Flume
  gallons into `flow_readings`; attribute metered water to zones by run window.
- **Phase 2 — dashboards.** Point Grafana at the SQLite/Postgres store for
  cross-property views.
- **Phase 3 — closed loop.** Push transformed moisture readings to Flex Daily zones
  via `setMoisturePercent`, or run our own schedule using Rachio as valves.

## Layout

```
src/watertool/
  config.py            env-driven settings (secrets stay secret)
  util.py              time helpers (ISO-8601 UTC everywhere)
  rachio/
    client.py          REST client (reads + control), rate-limit aware
    models.py          typed account tree (no PII stored)
    events.py          normalize 3 event shapes -> pair into runs (pure)
    gallons.py         nozzle-based gallons estimate
    webhooks.py        inbound signature / basic-auth verification
  db/
    schema.sql         SQLite schema (Postgres-portable)
    store.py           persistence + the ingest->reprocess pipeline
  ingest/receiver.py   FastAPI webhook receiver
  jobs/                backfill, reconcile, shared helpers
  cli.py               `watertool` entry point
tests/                 36 tests, fully offline
```
