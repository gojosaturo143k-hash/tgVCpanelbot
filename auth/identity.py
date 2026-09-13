"""Application identity: Firebase UID + Telegram ID -> one stable VC user.

Identity precedence
-------------------
* Telegram id is the **stable external identity**. When present the app user id
  is deterministic: ``tg_<telegram_id>``. Re-opening the Mini App, signing in
  with a new anonymous Firebase user or clearing browser storage therefore all
  resolve to the *same* VC identity - one Telegram account cannot casually
  become several participants.
* Without Telegram (dev / plain web) the identity is ``fb_<firebase_uid>``.

Roles are computed here, server side, from configuration only.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from config import config
from telegram.identity import TelegramProfile

ROLE_OWNER = "owner"
ROLE_MODERATOR = "moderator"
ROLE_PARTICIPANT = "participant"
ROLES = (ROLE_OWNER, ROLE_MODERATOR, ROLE_PARTICIPANT)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PALETTE = ("6366f1", "ec4899", "f59e0b", "10b981", "06b6d4", "8b5cf6", "ef4444", "14b8a6")


class IdentityError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fallback_avatar(user_id: str, display_name: str) -> str:
    seed = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    color = _PALETTE[int(seed[:2], 16) % len(_PALETTE)]
    initials = (display_name or "?").strip()[:2].upper() or "?"
    return f"https://ui-avatars.com/api/?background={color}&color=fff&bold=true&name={initials}"


@dataclass
class AppUser:
    """The linked identity used everywhere else in the backend."""

    id: str
    firebase_uid: str
    telegram_id: Optional[str]
    display_name: str
    username: Optional[str]
    photo_url: Optional[str]
    language_code: Optional[str] = None
    source: str = "web"          # "telegram" | "web" | "dev"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def avatar_url(self) -> str:
        return self.photo_url or fallback_avatar(self.id, self.display_name)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "telegramId": self.telegram_id,
            "displayName": self.display_name,
            "username": self.username,
            "photoUrl": self.photo_url,
            "avatarUrl": self.avatar_url,
            "source": self.source,
        }


class IdentityStore:
    """In-memory identity registry (swap for Firestore later if needed)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: Dict[str, AppUser] = {}
        self._telegram_to_id: Dict[str, str] = {}

    def link(
        self,
        firebase_uid: str,
        telegram: Optional[TelegramProfile],
        fallback_name: Optional[str] = None,
    ) -> AppUser:
        with self._lock:
            if telegram is not None:
                user_id = f"tg_{telegram.telegram_id}"
                existing = self._by_id.get(user_id)
                if existing is None:
                    existing = AppUser(
                        id=user_id,
                        firebase_uid=firebase_uid,
                        telegram_id=telegram.telegram_id,
                        display_name=telegram.display_name,
                        username=telegram.username,
                        photo_url=telegram.photo_url,
                        language_code=telegram.language_code,
                        source="telegram",
                    )
                    self._by_id[user_id] = existing
                    self._telegram_to_id[telegram.telegram_id] = user_id
                else:
                    # Refresh the profile; identity stays anchored to Telegram.
                    existing.firebase_uid = firebase_uid
                    existing.display_name = telegram.display_name
                    existing.username = telegram.username
                    existing.photo_url = telegram.photo_url
                    existing.language_code = telegram.language_code
                    existing.source = "telegram"
                    existing.updated_at = time.time()
                return existing

            user_id = f"fb_{hashlib.sha256(firebase_uid.encode()).hexdigest()[:20]}"
            existing = self._by_id.get(user_id)
            name = _clean_name(fallback_name) or f"Guest {user_id[-4:]}"
            if existing is None:
                existing = AppUser(
                    id=user_id,
                    firebase_uid=firebase_uid,
                    telegram_id=None,
                    display_name=name,
                    username=None,
                    photo_url=None,
                    source="dev" if config.DEV_MODE else "web",
                )
                self._by_id[user_id] = existing
            else:
                existing.display_name = name
                existing.updated_at = time.time()
            return existing

    def get(self, user_id: str) -> Optional[AppUser]:
        with self._lock:
            return self._by_id.get(user_id)

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)


def _clean_name(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).strip()[:32]


def resolve_role(user: AppUser, room_owner_id: Optional[str] = None) -> str:
    """Compute the role from server-side configuration only.

    A browser can never influence this: the only inputs are the verified
    Telegram id, the configured owner/moderator lists and the room's own
    recorded owner.
    """
    if room_owner_id and user.id == room_owner_id:
        return ROLE_OWNER
    if user.telegram_id:
        if user.telegram_id in config.OWNER_TELEGRAM_IDS:
            return ROLE_OWNER
        if user.telegram_id in config.MODERATOR_TELEGRAM_IDS:
            return ROLE_MODERATOR
    return ROLE_PARTICIPANT


def can_claim_ownerless_room(user: AppUser) -> bool:
    """Whether a room with no owner may be adopted by this user.

    In production ownership must come from configuration - a random browser
    connection never becomes the owner. In DEV_MODE the first joiner adopts the
    room so the stack is usable locally.
    """
    if user.telegram_id and user.telegram_id in config.OWNER_TELEGRAM_IDS:
        return True
    return bool(config.DEV_MODE)


identity_store = IdentityStore()
