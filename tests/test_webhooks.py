import base64
import hashlib
import hmac

from watertool.rachio.webhooks import verify_basic, verify_hmac, verify_request

SECRET = "test-token"
BODY = b'{"eventType":"DEVICE_ZONE_RUN_COMPLETED_EVENT"}'


def _hex(body, secret):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _b64(body, secret):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_hmac_hex_accepted():
    assert verify_hmac(BODY, _hex(BODY, SECRET), SECRET)


def test_hmac_base64_accepted():
    assert verify_hmac(BODY, _b64(BODY, SECRET), SECRET)


def test_hmac_sha256_prefix_accepted():
    assert verify_hmac(BODY, f"sha256={_hex(BODY, SECRET)}", SECRET)


def test_hmac_rejects_wrong_signature():
    assert not verify_hmac(BODY, _hex(BODY, "wrong"), SECRET)
    assert not verify_hmac(BODY, "", SECRET)
    assert not verify_hmac(BODY, _hex(b"other", SECRET), SECRET)


def test_basic_auth():
    header = "Basic " + base64.b64encode(b"rachio:pw").decode()
    assert verify_basic(header, "rachio", "pw")
    assert not verify_basic(header, "rachio", "nope")
    assert not verify_basic(None, "rachio", "pw")


def test_verify_request_none_mode():
    ok, method = verify_request({}, BODY, mode="none", token=SECRET,
                                basic_user="u", basic_pass="p")
    assert ok and method == "none"


def test_verify_request_hmac_present():
    headers = {"X-Signature": _hex(BODY, SECRET)}
    ok, method = verify_request(headers, BODY, mode="hmac", token=SECRET,
                                basic_user="u", basic_pass="p")
    assert ok and method == "hmac"


def test_verify_request_hmac_missing_header():
    ok, method = verify_request({}, BODY, mode="hmac", token=SECRET,
                                basic_user="u", basic_pass="p")
    assert not ok and method == "hmac_missing"


def test_verify_request_basic_mode():
    headers = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    ok, method = verify_request(headers, BODY, mode="basic", token=SECRET,
                                basic_user="u", basic_pass="p")
    assert ok and method == "basic"
