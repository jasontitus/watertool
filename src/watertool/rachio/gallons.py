"""Estimate gallons applied by a zone run from its nozzle config.

Rachio's public API never reports metered gallons unless a wired flow meter is
installed, so — exactly like the Rachio app's own usage screens — we compute it:

    gallons = precip_rate_in_per_hr * hours * area_sqft * 0.6233

0.6233 is US gallons per square foot per inch of applied water. This is the water
that LEAVES the nozzles, which is what a water bill charges for. Zone `efficiency`
(distribution uniformity) describes how much reaches the root zone, not how much
flows through the pipe, so it does NOT belong in a consumption estimate — Rachio
already bakes efficiency into the *runtime* it schedules.

These numbers are only as good as the per-zone nozzle precip rate and area, which
are user-entered. Until they're calibrated (catch-cup test, or regression against
a Flume), treat the output as a consistent relative signal, not billing truth.
"""

from __future__ import annotations

GALLONS_PER_SQFT_INCH = 0.6233


def estimate_gallons(
    inches_per_hour: float | None,
    area_sqft: float | None,
    duration_seconds: float | None,
) -> float | None:
    """Return estimated gallons, or None if any input is missing/non-positive."""
    if not inches_per_hour or not area_sqft or not duration_seconds:
        return None
    if inches_per_hour <= 0 or area_sqft <= 0 or duration_seconds <= 0:
        return None
    hours = duration_seconds / 3600.0
    return inches_per_hour * hours * area_sqft * GALLONS_PER_SQFT_INCH
