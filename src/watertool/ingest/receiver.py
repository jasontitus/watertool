"""FastAPI webhook receiver.

Policy (see webhooks.py): every inbound request is STORED, and we always answer
2xx so Rachio never auto-deregisters the webhook after 10 consecutive failures.
Runs are only ever rebuilt from verified events (store enforces this), so a
signing misconfiguration degrades to "retained, reprocess later" rather than data
loss or accepting forgeries.

create_app(settings, store) is a factory so tests can inject an in-memory store.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..db.store import Store
from ..rachio import events as ev
from ..rachio.webhooks import verify_request
from ..util import to_iso, utcnow
from ..web.routes import register_web

log = logging.getLogger("watertool.receiver")


def create_app(settings: Settings, store: Store) -> FastAPI:
    app = FastAPI(title="watertool", version="0.1.0")
    register_web(app, store)  # dashboard at "/" and /data.json

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "time": to_iso(utcnow())}

    @app.post(settings.webhook_path)
    async def rachio_webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        ok, method = verify_request(
            dict(request.headers),
            raw,
            mode=settings.webhook_verify,
            token=settings.token,
            basic_user=settings.webhook_basic_user,
            basic_pass=settings.webhook_basic_pass.get_secret_value(),
        )
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("rachio webhook: non-JSON body (%d bytes)", len(raw))
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=200)

        events, provider = ev.parse_webhook(body)
        signature_ok = True if method == "none" else ok
        touched_devices: set[str] = set()
        new_count = 0
        for e in events:
            if store.record_event(e, signature_ok=signature_ok):
                new_count += 1
            if e.device_id and (ok or method == "none"):
                touched_devices.add(e.device_id)

        if not ok and method != "none":
            log.warning("rachio webhook: signature NOT verified (method=%s); stored only", method)

        # Rebuild runs only for devices whose events we trust, over a short window.
        for device_id in touched_devices:
            store.reprocess_device_runs(device_id, lookback_days=7)

        return JSONResponse(
            {"ok": True, "provider": provider, "events": len(events),
             "new": new_count, "verified": ok, "method": method},
            status_code=200,
        )

    @app.post("/webhooks/ecowitt")
    async def ecowitt_webhook(request: Request) -> JSONResponse:
        """Phase-2 stub: Ecowitt gateways local-push form-encoded weather data.

        Parses the documented soilmoistureN fields into sensor_readings so the
        soil-sensor path lands in the same store. Mapping sensor channels to zones
        is left to a later config step.
        """
        form = await request.form()
        now = to_iso(utcnow())
        stored = 0
        with store._conn() as c:  # noqa: SLF001 (intentional internal use)
            for key, value in form.items():
                if not key.startswith("soilmoisture"):
                    continue
                try:
                    val = float(value)
                except (TypeError, ValueError):
                    continue
                c.execute(
                    """
                    INSERT INTO sensor_readings
                        (property_id, device_id, zone_id, source, sensor_id, metric, value, unit, timestamp, received_at)
                    VALUES (NULL, NULL, NULL, 'ecowitt', ?, 'soil_moisture_pct', ?, '%', ?, ?)
                    """,
                    (key, val, form.get("dateutc") or now, now),
                )
                stored += 1
        return JSONResponse({"ok": True, "stored": stored}, status_code=200)

    return app
