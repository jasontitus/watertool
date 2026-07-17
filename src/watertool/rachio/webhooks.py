"""Inbound webhook authentication.

Two Rachio webhook systems, two auth schemes:

  * New WebhookService signs each POST with `x-signature` = HMAC-SHA256 over the
    request body, using your Rachio API token as the secret. Rachio's exact
    canonicalization isn't fully pinned down in the public docs, so we verify
    against the raw body bytes and accept either hex or base64 encoding (with or
    without a "sha256=" prefix). The receiver stores the raw body + signature for
    every request, so if the real scheme turns out to differ we can adjust and
    re-verify from stored data without losing events.

  * Legacy webhooks carry HTTP Basic credentials embedded in the registered URL,
    which arrive as an Authorization header.

verify_request() returns (ok, method) and never raises. The receiver's policy is
to STORE every request regardless, but only build zone runs from verified events
(unless WEBHOOK_VERIFY=none) — so a signing misconfiguration degrades to "data
retained, reprocess later", never "silently accept forgeries" or "lose data".
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def _hmac_candidates(raw_body: bytes, secret: str) -> set[str]:
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    return {mac.hexdigest(), base64.b64encode(mac.digest()).decode("ascii")}


def verify_hmac(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not secret:
        return False
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[len("sha256="):].strip()
    return any(hmac.compare_digest(provided, c) for c in _hmac_candidates(raw_body, secret))


def verify_basic(
    authorization_header: str | None, user: str, password: str
) -> bool:
    if not authorization_header or not authorization_header.lower().startswith("basic "):
        return False
    encoded = authorization_header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    got_user, _, got_pass = decoded.partition(":")
    return hmac.compare_digest(got_user, user) and hmac.compare_digest(got_pass, password)


def verify_request(
    headers: dict[str, str],
    raw_body: bytes,
    *,
    mode: str,
    token: str,
    basic_user: str,
    basic_pass: str,
) -> tuple[bool, str]:
    """Return (verified, method_used). Case-insensitive header lookup."""
    h = {k.lower(): v for k, v in headers.items()}

    if mode == "none":
        return True, "none"

    signature = h.get("x-signature")
    if signature is not None:
        return verify_hmac(raw_body, signature, token), "hmac"

    if mode == "basic":
        return verify_basic(h.get("authorization"), basic_user, basic_pass), "basic"

    # mode == "hmac" but no signature header present
    return False, "hmac_missing"
