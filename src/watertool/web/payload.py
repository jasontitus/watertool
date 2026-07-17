"""Build the single JSON payload the dashboard consumes.

One builder serves both delivery modes:
  * live  — FastAPI serves it at /data.json from the local store
  * static — `watertool export` writes it next to dashboard.html for hosting

`anonymize=True` (the default for anything leaving the machine) strips the parts
that identify a real home: house numbers, full addresses, and internal Rachio ids
are removed; properties keep only a short street-name label. Watering stats stay,
so the dashboard is still useful, but a public URL no longer discloses where you
live or which house is vacant.
"""

from __future__ import annotations

import re

from ..db.store import Store
from ..util import utcnow


def anonymize_label(name: str | None) -> str:
    """"123 Oak Avenue" -> "Oak Avenue" (drop the house number)."""
    if not name:
        return "Property"
    return re.sub(r"^\s*\d+\s+", "", name).strip() or name


def build_payload(store: Store, anonymize: bool = False) -> dict:
    overview = store.property_overview()
    monthly = store.monthly_by_property(12)
    zones = store.zone_usage(None, 60)
    runs = store.recent_runs(60)

    if anonymize:
        # stable short id per property, in the same name order the UI sorts by
        id_map = {p["id"]: f"p{i}" for i, p in enumerate(overview)}
        name_map = {p["name"]: anonymize_label(p["name"]) for p in overview}
        for p in overview:
            p["id"] = id_map.get(p["id"], p["id"])
            p["name"] = name_map.get(p["name"], anonymize_label(p["name"]))
            p["address"] = None
            for ctl in p["controllers"]:
                ctl["name"] = None  # controller names embed a serial suffix
        for r in monthly:
            r["pid"] = id_map.get(r["pid"], r["pid"])
            r["property"] = name_map.get(r["property"], anonymize_label(r["property"]))
        for z in zones:
            z["pid"] = id_map.get(z["pid"], z["pid"])
            z["property"] = name_map.get(z["property"], anonymize_label(z["property"]))
        for r in runs:
            r["property"] = name_map.get(r["property"], anonymize_label(r["property"]))

    return {
        "generated_at": utcnow().isoformat(),
        "anonymized": anonymize,
        "overview": overview,
        "monthly": monthly,
        "zones": zones,
        "runs": runs,
    }
