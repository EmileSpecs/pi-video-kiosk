"""Signed session cookies for the admin site."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

COOKIE = "pikiosk_session"


def sign(payload: dict[str, Any], secret: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{digest}"


def unsign(token: str, secret: str) -> dict[str, Any] | None:
    if "." not in token:
        return None
    body, digest = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    padded = body + "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def new_session() -> dict[str, Any]:
    return {"csrf": secrets.token_hex(16), "ok": True}
