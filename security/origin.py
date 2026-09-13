"""Origin validation for HTTP (CORS) and the websocket handshake."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from config import config


def normalize(origin: Optional[str]) -> str:
    return (origin or "").strip().rstrip("/")


def is_allowed(origin: Optional[str]) -> bool:
    """True when the origin may talk to this backend."""
    value = normalize(origin)
    if not value:
        # Non browser clients (curl, tests, native apps) send no Origin header.
        return True
    if value in config.allowed_origins():
        return True
    try:
        host = urlparse(value).hostname or ""
    except ValueError:
        return False
    if host in ("localhost", "127.0.0.1") and config.DEV_MODE:
        return True
    # Telegram Mini Apps run inside *.telegram.org
    return host.endswith(".telegram.org")


def cors_headers(origin: Optional[str]) -> dict:
    value = normalize(origin)
    if not value or not is_allowed(value):
        return {}
    return {
        "Access-Control-Allow-Origin": value,
        "Vary": "Origin",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Max-Age": "600",
    }
