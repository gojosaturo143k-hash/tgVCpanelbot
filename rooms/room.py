"""Room aggregate. All state mutations go through this object under its lock."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from typing import Any, Dict, Iterable, List, Optional

from auth.identity import ROLE_MODERATOR, ROLE_OWNER, ROLE_PARTICIPANT
from config import config

from .participant import Participant

_ROLE_ORDER = {ROLE_OWNER: 0, ROLE_MODERATOR: 1, ROLE_PARTICIPANT: 2}


class Room:
    def __init__(
        self,
        room_id: str,
        title: str,
        emoji: str,
        *,
        live: bool = True,
        locked: bool = False,
        owner_id: Optional[str] = None,
        max_participants: Optional[int] = None,
        persistent: bool = False,
    ) -> None:
        self.id = room_id
        self.title = title
        self.emoji = emoji
        self.live = live
        self.locked = locked
        self.owner_id = owner_id
        self.max_participants = max_participants or config.MAX_PARTICIPANTS
        self.persistent = persistent

        self.created_at = time.time()
        self.ended = False
        self.empty_since: Optional[float] = time.time()

        self.lock = threading.RLock()
        self._participants: Dict[str, Participant] = {}
        self._messages: deque = deque(maxlen=config.MAX_CHAT_HISTORY)
        self._cooldowns: Dict[str, float] = {}

    # --------------------------------------------------------------- reads
    @property
    def participant_count(self) -> int:
        return len(self._participants)

    def participants(self) -> List[Participant]:
        return sorted(
            self._participants.values(),
            key=lambda p: (_ROLE_ORDER.get(p.role, 3), p.joined_at),
        )

    def get(self, user_id: str) -> Optional[Participant]:
        return self._participants.get(user_id)

    def is_full(self) -> bool:
        return len(self._participants) >= self.max_participants

    def cooldown_remaining(self, user_id: str) -> int:
        until = self._cooldowns.get(user_id, 0.0)
        remaining = until - time.time()
        if remaining <= 0:
            self._cooldowns.pop(user_id, None)
            return 0
        return int(remaining) + 1

    # -------------------------------------------------------------- writes
    def set_cooldown(self, user_id: str, seconds: int) -> None:
        self._cooldowns[user_id] = time.time() + seconds

    def add(self, participant: Participant) -> Participant:
        self._participants[participant.id] = participant
        self.empty_since = None
        return participant

    def remove(self, user_id: str) -> Optional[Participant]:
        participant = self._participants.pop(user_id, None)
        if not self._participants:
            self.empty_since = time.time()
        return participant

    def adopt_owner(self, user_id: str) -> bool:
        if self.owner_id is None:
            self.owner_id = user_id
            return True
        return self.owner_id == user_id

    def set_locked(self, locked: bool) -> None:
        self.locked = bool(locked)

    def end(self) -> None:
        self.ended = True
        self.live = False

    # ------------------------------------------------------ serialisation
    def info_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "roomId": self.id,
            "title": self.title,
            "emoji": self.emoji,
            "live": bool(self.live and not self.ended),
            "locked": bool(self.locked),
            "ownerId": self.owner_id,
            "participantCount": self.participant_count,
            "memberCount": self.participant_count,
            "maxParticipants": self.max_participants,
            "chatEnabled": config.CHAT_ENABLED,
        }

    def state_for(self, user_id: str) -> Dict[str, Any]:
        me = self.get(user_id)
        permissions = (
            me.permissions()
            if me
            else {
                "canMuteOthers": False,
                "canAllowSpeak": False,
                "canRestrict": False,
                "canRemove": False,
                "canModerate": False,
                "canLock": False,
                "canEnd": False,
                "canSpeak": False,
                "canChat": False,
            }
        )
        return {
            "room": self.info_dict(),
            "participants": [p.to_dict() for p in self.participants()],
            "currentUserId": user_id,
            "role": me.role if me else ROLE_PARTICIPANT,
            "permissions": permissions,
        }

    # ----------------------------------------------------------------- chat
    def add_message(self, sender: Participant, text: str) -> Dict[str, Any]:
        message = {
            "id": "msg_" + secrets.token_hex(8),
            "roomId": self.id,
            "userId": sender.id,
            "name": sender.name,
            "username": sender.user.username,
            "avatarUrl": sender.user.avatar_url,
            "role": sender.role,
            "text": text,
            "ts": int(time.time() * 1000),
        }
        self._messages.append(message)
        return message

    def history(self) -> List[Dict[str, Any]]:
        return list(self._messages)

    # ----------------------------------------------------------- broadcast
    def connections(self, exclude: Optional[Iterable[str]] = None) -> List[Any]:
        skip = set(exclude or ())
        return [
            p.connection
            for p in self._participants.values()
            if p.connection is not None and p.id not in skip
        ]
