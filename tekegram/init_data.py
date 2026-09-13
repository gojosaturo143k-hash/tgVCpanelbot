"""Verification of Telegram Mini App (Web App) initialisation data.

Algorithm (official):

    secret_key       = HMAC_SHA256(key="WebAppData", msg=<bot token>)
    data_check_string = "\\n".join(sorted("<k>=<v>" for k != "hash"))
    expected_hash     = HEX(HMAC_SHA256(key=secret_key, msg=data_check_string))

Only data that passes this check is treated as a real Telegram identity.
`telegram_id=123` posted by JavaScript is never accepted as proof of anything.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl

from config import config


class TelegramAuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


MAX_INIT_DATA_LENGTH = 8192


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def compute_hash(init_data: str, bot_token: str) -> str:
    """Expose the check-string hash so tests can build valid fixtures."""
    pairs = [(k, v) for k, v in parse_qsl(init_data, keep_blank_values=True) if k != "hash"]
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs, key=lambda kv: kv[0]))
    return hmac.new(_secret_key(bot_token), data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_init_data(init_data: str, bot_token: Optional[str] = None) -> Dict[str, Any]:
    """Verify signed init data and return its parsed fields."""
    token = bot_token if bot_token is not None else config.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramAuthError("TELEGRAM_NOT_CONFIGURED", "Telegram verification is not configured")
    if not isinstance(init_data, str) or not init_data.strip():
        raise TelegramAuthError("TELEGRAM_INIT_DATA_MISSING", "Telegram init data is required")
    if len(init_data) > MAX_INIT_DATA_LENGTH:
        raise TelegramAuthError("TELEGRAM_INIT_DATA_INVALID", "Telegram init data is too large")

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    provided_hash = fields.get("hash", "")
    if not provided_hash:
        raise TelegramAuthError("TELEGRAM_INIT_DATA_INVALID", "Telegram init data has no hash")

    expected = compute_hash(init_data, token)
    if not hmac.compare_digest(expected, provided_hash):
        raise TelegramAuthError("TELEGRAM_SIGNATURE_INVALID", "Telegram signature does not match")

    auth_date = fields.get("auth_date")
    try:
        auth_ts = int(auth_date or 0)
    except ValueError:
        raise TelegramAuthError("TELEGRAM_INIT_DATA_INVALID", "Telegram auth_date is malformed")
    age = time.time() - auth_ts
    if auth_ts <= 0 or age > config.TELEGRAM_INIT_DATA_TTL:
        raise TelegramAuthError("TELEGRAM_INIT_DATA_EXPIRED", "Telegram session is too old, reopen the app")
    if age < -300:
        raise TelegramAuthError("TELEGRAM_INIT_DATA_INVALID", "Telegram auth_date is in the future")

    raw_user = fields.get("user")
    if not raw_user:
        raise TelegramAuthError("TELEGRAM_USER_MISSING", "Telegram init data has no user")
    try:
        user = json.loads(raw_user)
    except ValueError as exc:
        raise TelegramAuthError("TELEGRAM_INIT_DATA_INVALID", "Telegram user payload is malformed") from exc
    if not isinstance(user, dict) or not user.get("id"):
        raise TelegramAuthError("TELEGRAM_USER_MISSING", "Telegram user id is missing")

    return {
        "user": user,
        "auth_date": auth_ts,
        "chat_instance": fields.get("chat_instance"),
        "chat_type": fields.get("chat_type"),
        "start_param": fields.get("start_param"),
        "query_id": fields.get("query_id"),
    }
