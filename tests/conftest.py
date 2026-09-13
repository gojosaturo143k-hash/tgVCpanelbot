"""Shared test bootstrap.

Sets a deterministic environment *before* `config` is imported, and provides
helpers for mocking Firebase and building genuinely signed Telegram init data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

TEST_BOT_TOKEN = "123456:TEST-BOT-TOKEN-NOT-REAL"

os.environ.setdefault("AUTH_SECRET", "unit-test-secret-not-production")
os.environ.setdefault("DEV_MODE", "false")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", TEST_BOT_TOKEN)
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:5173")
os.environ.setdefault("OWNER_TELEGRAM_IDS", "1000001")
os.environ.setdefault("MODERATOR_TELEGRAM_IDS", "1000002")
os.environ.setdefault("MAX_PARTICIPANTS", "20")
os.environ.setdefault("TICKET_TTL_SECONDS", "75")


def make_init_data(
    telegram_id: int,
    first_name: str = "Test",
    last_name: str = "User",
    username: Optional[str] = None,
    photo_url: Optional[str] = None,
    auth_date: Optional[int] = None,
    bot_token: str = TEST_BOT_TOKEN,
    tamper: bool = False,
) -> str:
    """Build correctly signed Telegram Mini App init data."""
    user: Dict[str, Any] = {"id": telegram_id, "first_name": first_name}
    if last_name:
        user["last_name"] = last_name
    if username:
        user["username"] = username
    if photo_url:
        user["photo_url"] = photo_url

    fields = {
        "user": json.dumps(user, separators=(",", ":")),
        "auth_date": str(auth_date or int(time.time())),
        "chat_type": "supergroup",
        "chat_instance": "-1234567890",
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if tamper:
        signature = ("0" * 64) if signature[0] != "0" else ("1" * 64)
    return urlencode({**fields, "hash": signature})


class FakeFirebase:
    """Stand-in for the Admin SDK so tests need no real credentials."""

    def __init__(self) -> None:
        self.valid = {}

    def token_for(self, uid: str) -> str:
        token = f"fake-firebase-token:{uid}"
        self.valid[token] = uid
        return token

    def install(self, monkeypatch_target) -> None:
        """Patch firebase.admin.verify_id_token in the given module namespace."""

        def _verify(id_token: str):
            from firebase.admin import FirebaseError

            uid = self.valid.get(id_token)
            if uid is None:
                raise FirebaseError("FIREBASE_TOKEN_INVALID", "Firebase token is not valid")
            return {"uid": uid, "provider": "anonymous", "anonymous": True, "verified": True}

        monkeypatch_target.verify_id_token = _verify
