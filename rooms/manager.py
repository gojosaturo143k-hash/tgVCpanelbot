"""The one and only room registry (in-memory, single instance)."""

from __future__ import annotations

import re
import threading
import time
from typing import Dict, List, Optional

from config import config

from .room import Room

_ROOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{3,48}$")


def valid_room_id(room_id: object) -> bool:
    return isinstance(room_id, str) and bool(_ROOM_ID_RE.match(room_id))


class RoomManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rooms: Dict[str, Room] = {}
        self._create_default_room()

    def _create_default_room(self) -> None:
        room = Room(
            room_id=config.DEFAULT_ROOM_ID,
            title=config.DEFAULT_ROOM_TITLE,
            emoji=config.DEFAULT_ROOM_EMOJI,
            live=True,
            locked=False,
            persistent=True,
        )
        self._rooms[room.id] = room

    # -------------------------------------------------------------- lookup
    def get(self, room_id: str) -> Optional[Room]:
        if not valid_room_id(room_id):
            return None
        with self._lock:
            return self._rooms.get(room_id)

    def get_or_create(self, room_id: str, title: Optional[str] = None) -> Optional[Room]:
        if not valid_room_id(room_id):
            return None
        with self._lock:
            room = self._rooms.get(room_id)
            if room is not None:
                return room
            if not config.ALLOW_ROOM_AUTOCREATE:
                return None
            room = Room(
                room_id=room_id,
                title=title or room_id.replace("-", " ").title(),
                emoji=config.DEFAULT_ROOM_EMOJI,
            )
            self._rooms[room_id] = room
            return room

    def all_rooms(self) -> List[Room]:
        with self._lock:
            return list(self._rooms.values())

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rooms = list(self._rooms.values())
        return {
            "rooms": len(rooms),
            "participants": sum(r.participant_count for r in rooms),
        }

    # ------------------------------------------------------------- cleanup
    def reset(self, room_id: str) -> None:
        """Wipe room state after the owner ends it."""
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                return
            if room.persistent:
                self._rooms[room_id] = Room(
                    room_id=room.id,
                    title=room.title,
                    emoji=room.emoji,
                    live=True,
                    locked=False,
                    persistent=True,
                )
            else:
                self._rooms.pop(room_id, None)

    def drop_empty(self) -> None:
        now = time.time()
        with self._lock:
            for room_id, room in list(self._rooms.items()):
                if room.persistent or room.participant_count:
                    continue
                if room.empty_since and now - room.empty_since > config.EMPTY_ROOM_TTL_SECONDS:
                    self._rooms.pop(room_id, None)


room_manager = RoomManager()
