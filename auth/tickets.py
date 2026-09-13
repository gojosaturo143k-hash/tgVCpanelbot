"""Signed session tokens and single-use WebSocket tickets.

* session token : ~1 hour, HMAC-SHA256 signed, sent as ``Authorization: Bearer``.
* ws ticket     : ~75 seconds, **single use**, appended to the socket URL
                  because browsers cannot set headers on the WS handshake.

A long-lived Firebase ID token never travels in a URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any, Dict, Optional

from config import config

_TOKEN_MAX_LENGTH = 4096


class TicketError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------- encoding --
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _sign(body: str) -> str:
    return _b64e(
        hmac.new(config.AUTH_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )


def _encode(payload: Dict[str, Any]) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def _decode(token: str) -> Dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > _TOKEN_MAX_LENGTH:
        raise TicketError("TOKEN_MALFORMED", "Token is malformed")
    body, signature = token.split(".", 1)
    if not hmac.compare_digest(_sign(body), signature):
        raise TicketError("TOKEN_SIGNATURE", "Token signature is invalid")
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TicketError("TOKEN_MALFORMED", "Token payload is unreadable") from exc
    if not isinstance(payload, dict):
        raise TicketError("TOKEN_MALFORMED", "Token payload is invalid")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise TicketError("TOKEN_MALFORMED", "Token has no expiry")
    if exp < time.time():
        raise TicketError("TOKEN_EXPIRED", "Token has expired")
    return payload


# ---------------------------------------------------------- single use log --
class _NonceLog:
    """Remembers consumed ticket ids until they would have expired anyway."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: Dict[str, float] = {}

    def consume(self, jti: str, expires_at: float) -> bool:
        now = time.time()
        with self._lock:
            if len(self._seen) > 4096:
                for key, exp in list(self._seen.items()):
                    if exp < now:
                        self._seen.pop(key, None)
            if jti in self._seen:
                return False
            self._seen[jti] = expires_at
            return True


_nonces = _NonceLog()


# ------------------------------------------------------------------- api ---
def issue_session(identity: Dict[str, Any], room_id: str) -> Dict[str, Any]:
    now = int(time.time())
    payload = {
        "typ": "session",
        "sub": identity["id"],
        "fbu": identity.get("firebase_uid", ""),
        "tg": identity.get("telegram_id"),
        "name": identity["display_name"],
        "un": identity.get("username"),
        "pic": identity.get("photo_url"),
        "src": identity.get("source", "web"),
        "room": room_id,
        "iat": now,
        "exp": now + config.SESSION_TTL_SECONDS,
        "jti": secrets.token_hex(8),
    }
    return {"token": _encode(payload), "payload": payload, "expiresIn": config.SESSION_TTL_SECONDS}


def verify_session(token: str) -> Dict[str, Any]:
    payload = _decode(token)
    if payload.get("typ") != "session":
        raise TicketError("TOKEN_TYPE", "Wrong token type")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise TicketError("TOKEN_MALFORMED", "Token has no subject")
    return payload


def issue_ticket(session_payload: Dict[str, Any], room_id: str) -> Dict[str, Any]:
    now = int(time.time())
    payload = {
        "typ": "ticket",
        "sub": session_payload["sub"],
        "tg": session_payload.get("tg"),
        "name": session_payload.get("name"),
        "un": session_payload.get("un"),
        "pic": session_payload.get("pic"),
        "src": session_payload.get("src", "web"),
        "room": room_id,
        "iat": now,
        "exp": now + config.TICKET_TTL_SECONDS,
        "jti": secrets.token_hex(12),
    }
    return {"ticket": _encode(payload), "payload": payload, "expiresIn": config.TICKET_TTL_SECONDS}


def redeem_ticket(token: str, room_id: str) -> Dict[str, Any]:
    """Verify + consume a ticket. A ticket works exactly once, for one room."""
    payload = _decode(token)
    if payload.get("typ") != "ticket":
        raise TicketError("TOKEN_TYPE", "Wrong token type")
    if payload.get("room") != room_id:
        raise TicketError("TICKET_ROOM_MISMATCH", "Ticket is not valid for this room")
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise TicketError("TOKEN_MALFORMED", "Ticket has no id")
    if not _nonces.consume(jti, float(payload["exp"])):
        raise TicketError("TICKET_ALREADY_USED", "Ticket has already been used")
    return payload


def peek(token: str) -> Optional[Dict[str, Any]]:
    """Non-consuming inspection, used by tests and diagnostics."""
    try:
        return _decode(token)
    except TicketError:
        return None
