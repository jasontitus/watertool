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
- **Dashboard** — a self-contained web UI (`watertool serve`) with a comparative
  monthly chart across residences, per-property cards, a zone breakdown, and a
  recent-runs table. Exportable as a static site (`watertool export`) for hosting,
  **anonymized by default**.
- Exposes Rachio's official control levers (manual runs, seasonal adjustment,
  schedule skip, rain delay, and Flex Daily `setMoisturePercent`) for future
  sensor-driven automation.

## Architecture

```
Rachio controllers ──webhooks──▶ receiver (FastAPI) ─┐
                   ──poll──────▶ reconciler ─────────┼─▶ events_raw ─▶ zone_runs ─┬─▶ report (CLI)
                                                      │      (ledger)    (paired)  ├─▶ dashboard (/ + /data.json)
external sensors (Ecowitt/YoLink) ──▶ sensor_readings┘   [phase 2]                └─▶ export → static site
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

## Dashboard

`watertool serve` also hosts a self-contained web dashboard at `/` (backed by
`/data.json` from the local store) — comparative monthly water use across residences,
per-property cards with sparklines, a top-zones breakdown, and a recent-runs table.
It's one HTML file with no external requests, theme-aware, using an
accessibility-validated (colorblind-safe) palette.

```bash
uv run watertool serve --host 127.0.0.1 --port 8000   # then open http://127.0.0.1:8000
```

Bind to `127.0.0.1` for local-only viewing — served this way it shows **real
addresses** and never leaves your machine.

To host it, export a **static** snapshot (works on Firebase Hosting, Netlify, S3,
GitHub Pages, any static host — the page just fetches `./data.json`):

```bash
uv run watertool export --out dist            # anonymized by default
uv run watertool export --out dist --full     # include full addresses
```

**Anonymized (the default for anything you host)** strips house numbers, full
addresses, GPS, controller serials, and internal ids — properties keep only a short
street-name label, so a public URL never discloses where you live or which house is
vacant. Watering stats are unaffected.

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
| `watertool export [--out D] [--full]` | Write a static dashboard (anonymized by default) |
| `watertool serve [--port N]` | Run the webhook receiver + dashboard |

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
- **Phase 2 — dashboards.** The built-in dashboard covers cross-property views;
  point Grafana at the SQLite/Postgres store for deeper ad-hoc analysis.
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
  web/
    dashboard.html     self-contained dashboard (inline SVG charts, no CDN)
    payload.py         builds /data.json (with anonymization)
    routes.py          serves / and /data.json
  jobs/                backfill, reconcile, shared helpers
  cli.py               `watertool` entry point
tests/                 40 tests, fully offline
```
