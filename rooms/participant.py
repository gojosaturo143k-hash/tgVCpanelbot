"""Participant model - the VC view of a linked application identity."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from auth.identity import ROLE_MODERATOR, ROLE_OWNER, ROLE_PARTICIPANT, AppUser


@dataclass
class Participant:
    user: AppUser
    role: str = ROLE_PARTICIPANT
    muted: bool = True
    speaking: bool = False
    restricted: bool = False
    joined: bool = True
    joined_at: float = field(default_factory=time.time)
    connection: Optional[Any] = None

    # ------------------------------------------------------------- helpers
    @property
    def id(self) -> str:
        return self.user.id

    @property
    def name(self) -> str:
        return self.user.display_name

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def can_moderate(self) -> bool:
        return self.role in (ROLE_OWNER, ROLE_MODERATOR)

    # -------------------------------------------------------- serialisation
    def to_dict(self) -> Dict[str, Any]:
        """Public shape consumed by the frontend. No credentials, ever."""
        return {
            "id": self.user.id,
            "userId": self.user.id,
            "name": self.user.display_name,
            "displayName": self.user.display_name,
            "username": self.user.username,
            "photoUrl": self.user.photo_url,
            "avatarUrl": self.user.avatar_url,
            "telegramId": self.user.telegram_id,
            "role": self.role,
            "muted": bool(self.muted),
            "speaking": bool(self.speaking and not self.muted),
            "restricted": bool(self.restricted),
            "joined": bool(self.joined),
            "joinedAt": int(self.joined_at * 1000),
        }

    def permissions(self) -> Dict[str, bool]:
        owner = self.role == ROLE_OWNER
        moderator = self.can_moderate
        return {
            "canMuteOthers": moderator,
            "canAllowSpeak": moderator,
            "canRestrict": moderator,
            "canRemove": moderator,
            "canModerate": moderator,
            "canLock": owner,
            "canEnd": owner,
            "canSpeak": not self.restricted,
            "canChat": not self.restricted,
        }
