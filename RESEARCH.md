# Rachio multi-property watering research

Researched 2026-07-17. Context: 4 properties, each with a Rachio controller. Goals: unified watering tracking, water-efficiency analysis, programmatic zone adjustment, optional per-zone moisture sensors.

**Bottom line:** The Rachio API is healthy and covers ~90% of what's needed, but has two hard gaps (no historical usage endpoint, no schedule/zone-config writes). Nothing existing aggregates watering across multiple properties/accounts — that's a genuine gap worth building. A thin custom system (webhook receiver + event poller + DB + Grafana) is a weekend-scale build; moisture sensors bolt on cleanly later via Ecowitt or YoLink and can even close the loop through Rachio's official `setMoisturePercent` endpoint.

---

## 1. The Rachio API

### Basics
- **v1 REST API**: `https://api.rach.io/1/public/...`; newer services (webhooks v2, hose timers, properties) at `https://cloud-rest.rach.io`. Docs: https://rachio.readme.io/ (machine-readable index at https://rachio.readme.io/llms.txt — append `.md` to any reference URL for raw markdown).
- **Auth**: static personal bearer token copied from the app (Account → API key). One token per account; covers every controller on the account. No scopes, no self-service rotation (rotation = PM Rachio staff).
- **Rate limit**: **3,500 calls/day per account**, reset midnight UTC, reported in `X-RateLimit-*` headers (https://rachio.readme.io/reference/rate-limiting). Home Assistant docs still say 1,700 — stale.
- **Program health**: Rain Bird acquired Rachio Oct 1, 2025; staff said no API changes expected (https://community.rachio.com/t/rainbird-merger-and-future-of-rachio-api/42176). Docs actively maintained; API confirmed working July 2026; rate limit was *raised* from 1,700 → 3,500. Staff engagement thin but real.

### What you can READ
- `GET /public/person/info` → account id; `GET /public/person/{id}` → full tree: all devices → zones, fixed schedules, flex schedules (one call = whole-account snapshot).
- Zone config is rich: `runtime`, `yardAreaSquareFeet`, `rootZoneDepth`, `efficiency`, `availableWater`, `depthOfWater`, `saturatedDepthOfWater`, `managementAllowedDepletion`, `customSoil`, `customCrop` (coefficient), `customNozzle.inchesPerHour`, `customShade`, `lastWateredDate`.
- `GET /public/device/{id}/event?startTime=&endTime=` → historical events (ZONE_STATUS, SCHEDULE_STATUS, DEVICE_STATUS, RAIN_DELAY). **Retention ~12 months** (staff statement) — archive now for year-over-year analysis.
- `GET /public/device/{id}/current_schedule` (what's running), `/forecast`.
- PropertyService at cloud-rest maps devices → home addresses (`listProperties/{userId}`).

### What you can WRITE (official)
| Lever | Endpoint |
|---|---|
| Start zone(s) with arbitrary durations | `POST /public/zone/start`, `/start_multiple` |
| Stop all watering | `POST /public/device/stop_water` |
| Pause/resume a run | `POST /public/device/pause_zone_run`, `resume_zone_run` |
| Run/skip a schedule | `POST /public/schedulerule/start`, `/skip` |
| Seasonal adjustment −100%..+100% per schedule | `PUT /public/schedulerule/seasonal_adjustment` |
| Rain delay | `POST /public/device/rain_delay` |
| Standby on/off | `POST /public/device/on` / `off` |
| Enable/disable zone | `POST /public/zone/enable` / `disable` |
| **Set Flex Daily soil moisture** | `PUT /public/zone/setMoisturePercent` (0–1) or `setMoistureLevel` (mm) |

The moisture setters were added explicitly for sensor integrations (2020). Semantics: 100% = field capacity, 0% = MAD floor — push a transformed sensor value to suppress/force Flex Daily runs. HA exposes this as `rachio.set_zone_moisture_percent`.

### What you CANNOT do (public API)
1. **No historical usage/gallons endpoint** — staff-confirmed. The app's usage screens come from undocumented cloud-rest endpoints (`/events/getWateringSummaryByInterval`, `/events/waterjournal/zone/...`). Gallons are computed client-side: `inchesPerHour × runtime(h) × sqft × 0.623`. **Your own store must be the system of record.**
2. **No schedule create/modify/delete** — read/run/skip/seasonal-adjust only.
3. **No zone-config writes** (runtime, nozzle, soil, area) — read-only.
4. **Moisture % is writable but not readable** (asymmetry): the live Flex Daily moisture level isn't in the public API.

### Unofficial APIs (fill the gaps, at risk)
- **cloud-rest.rach.io undocumented REST** (the web app's API, discoverable via devtools): `getDeviceState` (live running zone), `getWateringSummaryByInterval`, water journal, upcoming events. Personal token works for at least some.
- **gRPC `cloud.rach.io:443`** (the mobile app's API — there is *no* GraphQL; that's a myth). Fully reverse-engineered July 2026 by **rachio-mcp** (https://github.com/rwestergren/rachio-mcp) via APK decompilation: full schedule CRUD, zone-config writes (`UpdateAdvancedZone`, `UpdateZoneNozzles`), run history, and **`soil_moisture_level_at_end_of_day_pct`** per zone. Auth: email+password mints a long-lived token. Can break with any app release — use for nice-to-haves, not the backbone.

### Webhooks (the backbone for tracking)
Two coexisting systems:
- **Legacy v1** (`POST /public/notification/webhook`): basic-auth-in-URL only; payloads include `zoneId`, `zoneNumber`, `duration`, `durationInMinutes`, `flowVolume` (if a wired flow meter is present). Event types: ZONE_STARTED/STOPPED/COMPLETED/CYCLING, SCHEDULE_*, DEVICE_* (online/offline), rain delay, config deltas.
- **New WebhookService** (`cloud-rest.rach.io/webhook/createWebhook`): per-resource registration (max 10), **HMAC-SHA256 `x-signature`** using your API token as secret, 5 retries on 5xx, **auto-deregisters after 10 consecutive failed events** — monitor and re-register on a schedule. Event types: DEVICE_ZONE_RUN_STARTED/PAUSED/STOPPED/COMPLETED_EVENT, SCHEDULE_*_EVENT, valve/program events. https://rachio.readme.io/reference/webhooks
- Requirements: public HTTPS endpoint returning 2xx.
- Reliability: no chronic outages, but real friction (HA issue #147602 "realtime updates not working" open since June 2025; valve webhook quirks). **Design as at-least-once/lossy: reconcile with daily event polls.**
- Polled `/event` payloads are thinner than webhook payloads — duration/zone must be parsed from the English `summary` string or derived by pairing STARTED/COMPLETED timestamps (soak cycles complicate this).

### Multi-property mechanics
- One Rachio account holds multiple controllers across multiple home addresses ("properties"); `person/{id}` returns all of them; PropertyService maps device→address.
- **Guest/shared (Complete Access) devices are NOT supported for API integrations** — controllers must live on the token's own account.
- → **Consolidate all 4 controllers onto one account.** One token, one webhook config, one 3,500/day budget (plenty with webhook-first + hourly reconciliation ≈ a few hundred calls/day). Separate accounts would give 4× budget but no unified view.

---

## 2. Ecosystem — what exists

### Home Assistant
- **Core `rachio` integration**: switch per zone/schedule, rain-delay + standby switches, status sensors. Cloud **push** — HA must be internet-reachable (Nabu Casa or reverse proxy). **Multiple accounts confirmed supported** (one config entry per API key). Actions include `start_watering`, `start_multiple_zones`, `set_zone_moisture_percent`.
- Limitations: no usage/gallons/history sensors, no moisture sensor, **discards the webhook `flowVolume` field** (open feature request since 2023), realtime-updates bug #147602 open since June 2025.
- **biofects/rachio_local** (HACS, active 2026): polling-only alternative — no inbound webhook needed. Zone/schedule switches, last-watered, API-usage sensors. No gallons/moisture.
- **Smart Irrigation** (jeroenterheerdt/HAsmartirrigation, 533★, maintained): ET bucket model computes per-zone durations; fires events your automation forwards to `rachio.start_multiple_zones`. The proven "smarter than Rachio's weather intelligence" path inside HA.
- **Irrigation Unlimited** (rgc99, 450★): full scheduling engine driving any switch entity.
- Usage-tracking pattern in HA: history_stats/utility_meter on zone-switch on-time × measured GPM.

### Open-source projects (GitHub sweep, July 2026)
**No Prometheus exporter, no Influx exporter, no multi-account dashboard exists.** What does:

| Project | What | Status | Relevance |
|---|---|---|---|
| [rachio-mcp](https://github.com/rwestergren/rachio-mcp) | MCP server; reverse-engineered gRPC (schedule CRUD, moisture read!) | active 2026-06 | Install as LLM control plane; steal protos |
| [Taproot / smart-watering-system](https://github.com/jasonnickel-org/smart-watering-system) | Full replacement scheduler: ET soil model, 6-stage decision pipeline, MQTT→HA | v1.0 2026-03 | Best reference architecture |
| [hass-rachio-flume](https://github.com/oerbilgin/hass-rachio-flume) | Flume-based per-zone usage in HA | pre-alpha, active 2026-07 | Right idea, too early |
| [rachio-supervisor](https://github.com/NikolayS/rachio-supervisor) | CLI: schedules, skips, health summaries | early, 2026-06 | Watch |
| [valiquette/homebridge-rachio-irrigation](https://github.com/valiquette/homebridge-rachio-irrigation) | HomeKit bridge, webhooks, multi-home filtering | v1.5.2 2026-06 | If HomeKit wanted |
| [lnjustin/Rachio-Community](https://github.com/lnjustin/Rachio-Community) | Hubitat; monthly summaries via undocumented API | 2025-07 | Proof the summaries are scrapable |
| [rachiopy](https://github.com/rfverbruggen/rachiopy) | Python client (used by HA) | dormant 2024-01 | API stable; usable, or just hit REST |
| boatmeme/rachio (Node), Go clients | clients | dead | Skip |

### Commercial / first-party
- **Rachio "Pro Properties"**: free contractor-oriented feature in the Rachio app — all controllers across properties under one login; dashboard of alerts, running state, weather skips, flow anomalies. **Try this first for the "unified status" itch** — it's $0 and may cover monitoring (not analytics). https://support.rachio.com/en_us/pro-properties-H17xeEFLi
- **Barranca Verde**: only third-party SaaS on the Rachio API — Rachio+Flume per-zone gallons attribution, leak detection. $5/mo or $50/yr for 1-year retention; homeowner/single-home oriented. Their architecture writeup is the canonical Flume-attribution method: https://www.barrancaverde.com/blog/rachio-flume-integration-guide/
- **OpenSprinkler**: fully-local hardware endgame if Rachio's cloud ever becomes intolerable. Not worth swapping 4 working controllers now.

### Instructive real-world builds
1. Barranca Verde's method: webhook zone windows × Flume minute-flow = actual gallons/zone, no calibration (Flume lags 3–6h).
2. Taproot: disabled Rachio scheduling entirely, runs its own ET engine, uses Rachio as a valve actuator (Decision→Command→Verify with live rain check).
3. HA forum: $20 flow sensor + ESP32/ESPHome instead of Rachio's $150 discontinued meter.
4. Ecowitt WH51 → `setMoisturePercent` closed-loop beta (June 2026): https://community.rachio.com/t/i-built-an-ecowitt-rachio-integration-for-vegetable-gardens-looking-for-a-few-beta-testers/42489

---

## 3. Moisture sensors

**Constraint (confirmed):** Rachio 3's sensor terminals accept only binary dry-contact rain/soil/freeze sensors + wired flow meters. No analog moisture, no wireless pairing. → Granular moisture must flow through software; the officially blessed hook back into Rachio is `setMoisturePercent` (Flex Daily only).

| Option | Per-property (~5 zones) | Hub | Range | Battery | Integration | Babysitting |
|---|---|---|---|---|---|---|
| **Ecowitt WH51** ($18) + GW1200/GW3000 gateway ($32–55) | **~$122–163** | yes | ~100 m open, 915 MHz | 1×AA, ~1 yr | HA local push / ecowitt2mqtt / **cloud API v3 (one account = all 4 properties)** | Low; one-time AD calibration |
| **YoLink YS8009** solar 3-in-1 ($36–50, IP66, new 2025) + hub ($24–30) | **~$205–275** | yes | **LoRa 1,000 ft+** | **solar, none** | HA core (merged 2025-07) / free HTTP+MQTT API | Lowest |
| Zigbee (Third Reality Gen2 ~$20, Tuya ~$15–25) | ~$125–150 + HA box | Zigbee coordinator + host per property | tens of m, walls hurt | AA, 1–3 yr | Z2M/ZHA | Highest outdoors; Tuya accuracy poor |
| Netro Whisperer 2 ($80, solar, Wi-Fi, public API 2k calls/day) | ~$400 | no | yard Wi-Fi | solar | Netro API / HACS | Low, cloud-poll |
| LoRaWAN (Dragino SE01-LB ~$150 + gateway $150–280) | ~$900–1,100 | LoRaWAN gw + TTN/ChirpStack | km | 5–10 yr | MQTT | High setup |
| Pro tier (Irrometer/Sentek) | $930–2,500+ | proprietary loggers | — | yrs | vendor exports | Install labor |

**Recommendation:** (a) easy default = **Ecowitt WH51 fleet** (cheapest, community standard; Ecowitt cloud API v3 aggregates all four properties under one account — no on-site compute needed); (b) large-property/no-babysitting variant = **YoLink YS8009** (solar, ¼-mile LoRa, one cloud API for everything, ~$80–130 more per property). Skip Zigbee and DIY LoRaWAN.

## 4. Measuring actual gallons

- **Flume 2/F2X ($269)**: strap-on whole-home meter sensor, no plumbing, **free personal REST API** + official HA integration. Attribute irrigation by intersecting minute-flow with zone-run windows (Barranca Verde method). The pragmatic pick.
- **DAE MJ-75c submeter ($93, 1 gal/pulse) + ESP32/ESPHome or Shelly (~$110–125 + plumber)**: legal-grade per-line truth on the irrigation main.
- **YoLink FlowSmart YS5018 ($255–300)**: ultrasonic meter+shutoff, 10-yr battery, same YoLink hub/API as the soil sensors.
- **Rachio's wireless flow meter: discontinued.** Wired flow meters still work (per-zone `flowVolume` lands in webhooks).
- Moen Flo / Phyn: no official APIs; skip.
- Without any meter: computed gallons from zone config (`inchesPerHour × hours × sqft × 0.623`) — fine for trends if nozzle rates/areas are calibrated per zone; it's what the Rachio app shows anyway.

---

## 5. Build vs adopt — assessment

**Adopt-only paths and why they fall short:**
- *Rachio Pro Properties* (free): unified live status/alerts across the 4 properties, but no analytics/history/efficiency tooling. Worth enabling regardless.
- *Home Assistant*: gets zone control + multi-account under one roof quickly, but needs internet-reachable HA, drops `flowVolume`, has an open realtime bug, and provides no usage history — you'd end up building the tracking layer anyway (history_stats × GPM templates).
- *Barranca Verde* ($5/mo): closest existing product, single-home oriented, their retention is 1 year, and it's someone else's roadmap.

**Build case:** the exact want — cross-property watering history + efficiency analytics — does not exist, the public API can't backfill it later (12-month event retention, no usage endpoint), and the build is small because webhooks do the heavy lifting.

### Proposed watertool architecture
1. **One Rachio account** holding all 4 controllers (API ignores guest-shared devices). One token, env-var only.
2. **Ingest**: tiny HTTPS webhook receiver (FastAPI on Fly/Cloud Run/VPS) — verify HMAC `x-signature`, store raw JSON, upsert `zone_runs` (start/complete pairing, soak-cycle aware, capture `flowVolume` if ever present).
3. **Reconciler**: hourly `person/{id}` snapshot (zone-config history for gallons math + detecting setting drift) + daily `/device/{id}/event` sweep to catch dropped webhooks; re-register webhooks if missing (auto-deregistration guard). Budget ≈ 200–800 calls/day of 3,500.
4. **Backfill on day one**: pull the trailing ~12 months of events per device before they age out.
5. **Store**: Postgres (or SQLite to start): `properties`, `devices`, `zones` (config as slowly-changing dimension), `zone_runs`, `events_raw`, `sensor_readings`, `flow_readings`.
6. **Gallons**: computed per run from zone config; upgrade any property with a Flume for ground truth and per-zone attribution.
7. **Dashboard**: Grafana first (fastest path to cross-property views: gallons/week by property/zone, runtime vs ET, skip effectiveness); custom web UI later if wanted.
8. **Adjustment levers** (official only, to start): seasonal_adjustment, schedule skip, rain delay, `start_multiple` for custom runs, `setMoisturePercent` when sensors arrive. Keep rachio-mcp installed for ad-hoc LLM-driven control and as the escape hatch for schedule/zone-config writes.
9. **Sensors (phase 2)**: Ecowitt or YoLink cloud API → `sensor_readings` → dashboards; optional closed loop pushing transformed moisture to Flex Daily zones.
10. Two control philosophies, escalating: start by *nudging* Rachio's Flex Daily (moisture/seasonal levers); if ambitions grow, *own* scheduling entirely via `start_multiple` with Rachio as dumb valves (Taproot prior art).

### Gotchas checklist
- Webhooks are at-least-once and can silently die (10-failure auto-deregister) → reconcile + monitor.
- Polled events need summary-string parsing / timestamp pairing; webhook payloads are richer.
- ~12-month event retention → backfill immediately.
- Computed gallons are fiction until per-zone `inchesPerHour`/area are calibrated (catch-cup test or Flume regression).
- Moisture: writable officially, readable only via unofficial gRPC.
- One shared 3,500/day budget across all controllers; webhook-first keeps usage trivial.
- Rain Bird acquisition (Oct 2025): no changes announced; keep the reconciler defensive anyway.
