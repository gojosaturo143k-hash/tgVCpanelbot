"""Validation of everything that arrives from a client."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from config import config

USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MODERATION_ACTIONS = ("mute", "unmute", "allow", "restrict", "remove", "kick", "lock", "unlock")


class ValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sanitize_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).strip()[:limit]


def parse_frame(raw: object) -> Tuple[str, Dict[str, Any]]:
    """Parse one inbound websocket frame into (event, payload)."""
    if isinstance(raw, (bytes, bytearray)):
        raise ValidationError("INVALID_MESSAGE", "Binary frames are not supported")
    if not isinstance(raw, str):
        raise ValidationError("INVALID_MESSAGE", "Unsupported frame type")
    if len(raw.encode("utf-8", "ignore")) > config.MAX_WS_MESSAGE_BYTES:
        raise ValidationError("PAYLOAD_TOO_LARGE", "Message exceeds the size limit")
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        raise ValidationError("INVALID_JSON", "Message is not valid JSON")
    if not isinstance(data, dict):
        raise ValidationError("INVALID_MESSAGE", "Message must be a JSON object")

    event = data.get("type")
    if not isinstance(event, str) or not event or len(event) > 40:
        raise ValidationError("INVALID_TYPE", "Missing or invalid event type")

    payload = data.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValidationError("INVALID_PAYLOAD", "payload must be an object")
    return event, payload


def require_bool(payload: Dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValidationError("INVALID_PAYLOAD", f"'{key}' must be a boolean")
    return value


def optional_user_id(payload: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not USER_ID_RE.match(value):
            raise ValidationError("INVALID_PAYLOAD", f"'{key}' is not a valid user id")
        return value
    return None


def require_user_id(payload: Dict[str, Any], *keys: str) -> str:
    value = optional_user_id(payload, *keys)
    if value is None:
        raise ValidationError("INVALID_PAYLOAD", f"'{keys[0]}' is required")
    return value


def validate_sdp(payload: Dict[str, Any], event: str) -> Dict[str, Any]:
    """Validate an SDP description. It is relayed byte-for-byte, never rewritten."""
    sdp = payload.get("sdp")
    if not isinstance(sdp, dict):
        raise ValidationError("INVALID_PAYLOAD", "'sdp' must be an object")

    sdp_type = sdp.get("type")
    if sdp_type not in ("offer", "answer", "pranswer", "rollback"):
        raise ValidationError("INVALID_PAYLOAD", "Unsupported SDP type")
    expected = "offer" if event == "OFFER" else "answer"
    if sdp_type not in (expected, "pranswer", "rollback"):
        raise ValidationError("INVALID_PAYLOAD", f"SDP type must be '{expected}'")

    body = sdp.get("sdp")
    if not isinstance(body, str) or not body.strip():
        raise ValidationError("INVALID_PAYLOAD", "SDP body is missing")
    if len(body.encode("utf-8", "ignore")) > config.MAX_SDP_BYTES:
        raise ValidationError("PAYLOAD_TOO_LARGE", "SDP exceeds the size limit")
    if "m=video" in body:
        raise ValidationError("VIDEO_NOT_ALLOWED", "This service is audio only")
    return {"type": sdp_type, "sdp": body}


def validate_ice(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidate = payload.get("candidate")
    if candidate is None:
        raise ValidationError("INVALID_PAYLOAD", "'candidate' is required")
    if not isinstance(candidate, dict):
        raise ValidationError("INVALID_PAYLOAD", "'candidate' must be an object")
    try:
        size = len(json.dumps(candidate))
    except (TypeError, ValueError):
        raise ValidationError("INVALID_PAYLOAD", "'candidate' is not serialisable")
    if size > config.MAX_ICE_BYTES:
        raise ValidationError("PAYLOAD_TOO_LARGE", "ICE candidate exceeds the size limit")

    out: Dict[str, Any] = {}
    value = candidate.get("candidate")
    if value is not None and not isinstance(value, str):
        raise ValidationError("INVALID_PAYLOAD", "Invalid ICE candidate string")
    out["candidate"] = value if isinstance(value, str) else ""

    mid = candidate.get("sdpMid")
    if mid is not None and not isinstance(mid, str):
        raise ValidationError("INVALID_PAYLOAD", "Invalid sdpMid")
    out["sdpMid"] = mid

    index = candidate.get("sdpMLineIndex")
    if index is not None and not isinstance(index, int):
        raise ValidationError("INVALID_PAYLOAD", "Invalid sdpMLineIndex")
    out["sdpMLineIndex"] = index

    ufrag = candidate.get("usernameFragment")
    if ufrag is not None:
        if not isinstance(ufrag, str):
            raise ValidationError("INVALID_PAYLOAD", "Invalid usernameFragment")
        out["usernameFragment"] = ufrag
    return out


def validate_chat_text(payload: Dict[str, Any]) -> str:
    if not config.CHAT_ENABLED:
        raise ValidationError("CHAT_DISABLED", "Chat is disabled in this room")
    raw = payload.get("text")
    if not isinstance(raw, str):
        raise ValidationError("INVALID_PAYLOAD", "'text' must be a string")
    # Chat is plain data. It is never parsed as a command.
    text = _CONTROL.sub("", raw).strip()
    if not text:
        raise ValidationError("EMPTY_MESSAGE", "Message is empty")
    if len(text) > config.MAX_CHAT_LENGTH:
        raise ValidationError(
            "MESSAGE_TOO_LONG", f"Message exceeds {config.MAX_CHAT_LENGTH} characters"
        )
    return text


def validate_action(payload: Dict[str, Any]) -> str:
    action = payload.get("action")
    if not isinstance(action, str) or action.lower() not in MODERATION_ACTIONS:
        raise ValidationError("INVALID_ACTION", "Unknown moderation action")
    return action.lower()
