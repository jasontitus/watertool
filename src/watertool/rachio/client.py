"""Thin synchronous Rachio REST client.

Covers the read + control surface watertool needs. Captures the X-RateLimit-*
headers off every response so jobs can watch the shared 3,500/day account budget.

User-Agent is a bare project token with NO contact info — outbound requests must
never carry personal identifiers.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

USER_AGENT = "watertool/0.1"


class RachioError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RachioRateLimited(RachioError):
    pass


class RachioClient:
    def __init__(
        self,
        token: str,
        *,
        base: str = "https://api.rach.io/1",
        cloud_rest_base: str = "https://cloud-rest.rach.io",
        min_interval: float = 0.5,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not token:
            raise RachioError("RACHIO_API_TOKEN is not set")
        self._base = base.rstrip("/")
        self._cloud = cloud_rest_base.rstrip("/")
        self._min_interval = min_interval
        self._last_call = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
        )
        self.rate_limit_limit: str | None = None
        self.rate_limit_remaining: str | None = None
        self.rate_limit_reset: str | None = None

    def __enter__(self) -> "RachioClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # --- plumbing ----------------------------------------------------------

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _capture_rate(self, r: httpx.Response) -> None:
        self.rate_limit_limit = r.headers.get("X-RateLimit-Limit", self.rate_limit_limit)
        self.rate_limit_remaining = r.headers.get("X-RateLimit-Remaining", self.rate_limit_remaining)
        self.rate_limit_reset = r.headers.get("X-RateLimit-Reset", self.rate_limit_reset)

    def _request(self, method: str, url: str, *, json: Any = None, retries: int = 2) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle()
            try:
                r = self._http.request(method, url, json=json)
            except httpx.HTTPError as exc:  # network error
                last_exc = exc
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RachioError(f"network error: {exc}") from exc
            self._capture_rate(r)
            if r.status_code == 429:
                raise RachioRateLimited("429 rate limit exceeded", status=429, body=r.text)
            if r.status_code >= 500 and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RachioError(
                    f"{r.status_code} {method} {url}", status=r.status_code, body=r.text
                )
            return r
        raise RachioError(f"request failed: {last_exc}")

    def _get(self, path: str) -> Any:
        return self._request("GET", f"{self._base}{path}").json()

    def _get_cloud(self, path: str) -> Any:
        return self._request("GET", f"{self._cloud}{path}").json()

    def _post(self, path: str, body: dict) -> httpx.Response:
        return self._request("POST", f"{self._base}{path}", json=body)

    def _put(self, path: str, body: dict) -> httpx.Response:
        return self._request("PUT", f"{self._base}{path}", json=body)

    # --- reads -------------------------------------------------------------

    def get_person_id(self) -> str:
        return self._get("/public/person/info")["id"]

    def get_person(self, person_id: str) -> dict:
        return self._get(f"/public/person/{person_id}")

    def get_account(self) -> dict:
        """Full account tree in one call (also spends only 2 calls total)."""
        return self.get_person(self.get_person_id())

    def get_device(self, device_id: str) -> dict:
        return self._get(f"/public/device/{device_id}")

    def get_device_events(self, device_id: str, start_ms: int, end_ms: int) -> list[dict]:
        return self._get(
            f"/public/device/{device_id}/event?startTime={start_ms}&endTime={end_ms}"
        )

    def get_current_schedule(self, device_id: str) -> dict:
        return self._get(f"/public/device/{device_id}/current_schedule")

    def list_properties(self, person_id: str) -> list[dict]:
        """Property service (cloud-rest): real house names + street addresses.

        Undocumented-but-stable endpoint the app uses. Returns [] if unavailable
        so discovery degrades to per-controller synthetic properties.
        """
        try:
            data = self._get_cloud(f"/property/listProperties/{person_id}")
        except RachioError:
            return []
        return data.get("property", []) if isinstance(data, dict) else []

    # --- control (official write surface) ----------------------------------

    def start_zone(self, zone_id: str, duration_seconds: int) -> httpx.Response:
        return self._put("/public/zone/start", {"id": zone_id, "duration": duration_seconds})

    def start_multiple(self, zones: list[dict]) -> httpx.Response:
        # zones: [{"id": zone_id, "duration": seconds, "sortOrder": n}, ...]
        return self._put("/public/zone/start_multiple", {"zones": zones})

    def stop_water(self, device_id: str) -> httpx.Response:
        return self._put("/public/device/stop_water", {"id": device_id})

    def rain_delay(self, device_id: str, duration_seconds: int) -> httpx.Response:
        return self._put("/public/device/rain_delay", {"id": device_id, "duration": duration_seconds})

    def set_zone_moisture_percent(self, zone_id: str, percent: float) -> httpx.Response:
        # percent is 0..1. 1.0 = field capacity ("skip next Flex Daily run"),
        # 0.0 = wilting point ("water now"). Only affects Flex Daily schedules.
        percent = max(0.0, min(1.0, percent))
        return self._put("/public/zone/setMoisturePercent", {"id": zone_id, "percent": percent})

    def set_seasonal_adjustment(self, schedule_rule_id: str, adjustment: float) -> httpx.Response:
        # adjustment is -1..1 (-100%..+100%).
        adjustment = max(-1.0, min(1.0, adjustment))
        return self._put(
            "/public/schedulerule/seasonal_adjustment",
            {"id": schedule_rule_id, "adjustment": adjustment},
        )

    def skip_schedule(self, schedule_rule_id: str) -> httpx.Response:
        return self._put("/public/schedulerule/skip", {"id": schedule_rule_id})

    # --- legacy webhooks ---------------------------------------------------

    def list_webhook_event_types(self) -> list[dict]:
        return self._get("/public/notification/webhook_event_type")

    def list_device_webhooks(self, device_id: str) -> list[dict]:
        return self._get(f"/public/notification/{device_id}/webhook")

    def create_webhook(
        self, device_id: str, url: str, event_type_ids: list[str], external_id: str
    ) -> dict:
        body = {
            "device": {"id": device_id},
            "externalId": external_id,
            "url": url,
            "eventTypes": [{"id": str(i)} for i in event_type_ids],
        }
        return self._post("/public/notification/webhook", body).json()

    def delete_webhook(self, webhook_id: str) -> httpx.Response:
        return self._request("DELETE", f"{self._base}/public/notification/webhook/{webhook_id}")
