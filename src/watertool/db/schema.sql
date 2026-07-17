-- watertool schema. SQLite to start; kept portable to Postgres (no SQLite-only
-- types, explicit timestamps as TEXT ISO-8601 UTC). Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS properties (
    id          TEXT PRIMARY KEY,          -- Rachio property id, or "dev:<device_id>"
    name        TEXT,
    address     TEXT,
    latitude    REAL,
    longitude   REAL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,        -- Rachio device id
    property_id   TEXT REFERENCES properties(id),
    name          TEXT,
    model         TEXT,
    serial_number TEXT,
    mac_address   TEXT,
    latitude      REAL,
    longitude     REAL,
    timezone      TEXT,
    status        TEXT,
    on_standby    INTEGER,                 -- 0/1
    raw_json      TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zones (
    id              TEXT PRIMARY KEY,      -- Rachio zone id
    device_id       TEXT NOT NULL REFERENCES devices(id),
    zone_number     INTEGER,
    name            TEXT,
    enabled         INTEGER,
    area_sqft       REAL,
    inches_per_hour REAL,
    efficiency      REAL,
    soil            TEXT,
    crop            TEXT,
    nozzle_head     TEXT,
    image_url       TEXT,
    raw_json        TEXT,
    updated_at      TEXT NOT NULL,
    UNIQUE (device_id, zone_number)
);

-- One row per zone watering. UNIQUE(zone_id, start_time) makes re-processing the
-- same events idempotent.
CREATE TABLE IF NOT EXISTS zone_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           TEXT,
    zone_id             TEXT,
    zone_number         INTEGER,
    zone_name           TEXT,
    schedule_id         TEXT,
    source              TEXT,              -- webhook_v2 | webhook_legacy | poll
    start_time          TEXT NOT NULL,     -- ISO-8601 UTC
    end_time            TEXT,
    duration_seconds    INTEGER,
    gallons_estimated   REAL,
    flow_volume_gallons REAL,              -- metered, if a flow meter is attached
    was_cycle_soak      INTEGER DEFAULT 0,
    complete            INTEGER DEFAULT 0,
    event_id_start      TEXT,
    event_id_complete   TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (zone_id, start_time)
);
CREATE INDEX IF NOT EXISTS idx_zone_runs_start ON zone_runs(start_time);
CREATE INDEX IF NOT EXISTS idx_zone_runs_device ON zone_runs(device_id, start_time);

-- Append-only ledger of every event we ever saw. dedup_key enforces at-least-once
-- ingestion (webhooks + overlapping polls) collapsing to exactly-once storage.
-- Normalized fields are denormalized onto the row so run rebuilding never re-parses.
CREATE TABLE IF NOT EXISTS events_raw (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key           TEXT NOT NULL UNIQUE,
    source              TEXT NOT NULL,     -- webhook_v2 | webhook_legacy | poll
    kind                TEXT,              -- canonical kind (ZONE_STARTED, ...)
    device_id           TEXT,
    zone_id             TEXT,
    zone_number         INTEGER,
    zone_name           TEXT,
    duration_seconds    INTEGER,
    flow_volume_gallons REAL,
    schedule_id         TEXT,
    timestamp           TEXT,              -- event time ISO-8601 UTC
    received_at         TEXT NOT NULL,
    signature_ok        INTEGER,
    body_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_raw_dev_ts ON events_raw(device_id, timestamp);

CREATE TABLE IF NOT EXISTS webhook_registrations (
    id          TEXT PRIMARY KEY,          -- Rachio webhook id
    device_id   TEXT,
    external_id TEXT,
    url         TEXT,
    provider    TEXT,                      -- legacy | v2
    event_types TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT
);

-- Phase-2 landing tables for external sensors / flow meters.
CREATE TABLE IF NOT EXISTS sensor_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT,
    device_id   TEXT,
    zone_id     TEXT,
    source      TEXT,                      -- ecowitt | yolink | netro | ...
    sensor_id   TEXT,
    metric      TEXT,                      -- soil_moisture_pct | soil_temp_c | battery_pct
    value       REAL,
    unit        TEXT,
    timestamp   TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sensor_ts ON sensor_readings(sensor_id, metric, timestamp);

CREATE TABLE IF NOT EXISTS flow_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT,
    source      TEXT,                      -- flume | dae | yolink
    gallons     REAL,
    flow_rate   REAL,
    timestamp   TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flow_ts ON flow_readings(source, timestamp);

CREATE TABLE IF NOT EXISTS poll_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
