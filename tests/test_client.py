import httpx
import pytest

from watertool.rachio.client import RachioClient, RachioError, RachioRateLimited

DEVICE = {"id": "d1", "name": "Home", "on": True, "zones": []}


def _client(handler) -> RachioClient:
    return RachioClient("tok", min_interval=0, transport=httpx.MockTransport(handler))


def test_get_account_and_rate_capture():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/public/person/info"):
            return httpx.Response(200, json={"id": "person-1"},
                                  headers={"X-RateLimit-Limit": "3500",
                                           "X-RateLimit-Remaining": "3499"})
        if request.url.path.endswith("/public/person/person-1"):
            return httpx.Response(200, json={"id": "person-1", "devices": [DEVICE]})
        return httpx.Response(404, text="not found")

    with _client(handler) as c:
        account = c.get_account()
        assert account["devices"][0]["id"] == "d1"
        assert c.rate_limit_remaining == "3499"
        assert c.rate_limit_limit == "3500"


def test_rate_limited_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with _client(handler) as c:
        with pytest.raises(RachioRateLimited):
            c.get_person_id()


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    with _client(handler) as c:
        with pytest.raises(RachioError):
            c.get_device("missing")


def test_missing_token_raises():
    with pytest.raises(RachioError):
        RachioClient("")


def test_set_moisture_percent_clamps_and_posts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(204)

    with _client(handler) as c:
        c.set_zone_moisture_percent("z1", 1.5)  # clamps to 1.0
    assert b'"percent":1.0' in seen["body"].replace(b" ", b"")
