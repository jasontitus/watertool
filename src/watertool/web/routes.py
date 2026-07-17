"""Dashboard routes for the live (FastAPI) mode.

Serves the same self-contained dashboard.html used for static hosting, plus
/data.json from the live store. The HTML fetches ./data.json relative to itself,
so the identical file works whether FastAPI serves it or Firebase does.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..db.store import Store
from .payload import build_payload

_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text()


def register_web(app: FastAPI, store: Store) -> None:
    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return _DASHBOARD_HTML

    @app.get("/data.json")
    def data() -> JSONResponse:
        # live mode shows real names locally; nothing leaves the machine
        return JSONResponse(build_payload(store, anonymize=False))
