"""Normalisation of a verified Telegram user into a VC profile.

Only fields Telegram legitimately provides through the signed Mini App context
are used. Nothing is fetched from, or invented about, a private account.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).strip()[:limit]


def _safe_photo_url(value: Any) -> Optional[str]:
    url = _clean(value, 512)
    if not url:
        return None
    if not url.startswith("https://"):
        return None
    return url


@dataclass(frozen=True)
class TelegramProfile:
    telegram_id: str
    first_name: str
    last_name: str
    username: Optional[str]
    photo_url: Optional[str]
    language_code: Optional[str]
    is_premium: bool
    allows_write: bool
    start_param: Optional[str] = None
    chat_instance: Optional[str] = None

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or (f"@{self.username}" if self.username else f"Telegram {self.telegram_id[-4:]}")

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "telegramId": self.telegram_id,
            "displayName": self.display_name,
            "firstName": self.first_name,
            "lastName": self.last_name,
            "username": self.username,
            "photoUrl": self.photo_url,
            "languageCode": self.language_code,
            "isPremium": self.is_premium,
        }


def build_profile(verified: Dict[str, Any]) -> TelegramProfile:
    """Build a profile from the output of ``verify_init_data``."""
    user = verified.get("user") or {}
    username = _clean(user.get("username"), 32)
    if username and not _USERNAME_RE.match(username):
        username = ""

    return TelegramProfile(
        telegram_id=str(user.get("id")),
        first_name=_clean(user.get("first_name"), 64),
        last_name=_clean(user.get("last_name"), 64),
        # Never invent a username when Telegram did not provide one.
        username=username or None,
        photo_url=_safe_photo_url(user.get("photo_url")),
        language_code=_clean(user.get("language_code"), 8) or None,
        is_premium=bool(user.get("is_premium")),
        allows_write=bool(user.get("allows_write_to_pm")),
        start_param=_clean(verified.get("start_param"), 64) or None,
        chat_instance=_clean(verified.get("chat_instance"), 64) or None,
    )
