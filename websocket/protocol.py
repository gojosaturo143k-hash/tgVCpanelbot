"""Wire protocol: every frame is {"type": "...", "payload": {...}}.

Event names are fixed by the existing frontend and must not be renamed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# --------------------------------------------------------- client -> server
JOIN_ROOM = "JOIN_ROOM"
LEAVE_ROOM = "LEAVE_ROOM"
MUTE_CHANGED = "MUTE_CHANGED"
SPEAKING_CHANGED = "SPEAKING_CHANGED"
MODERATE = "MODERATE"
END_ROOM = "END_ROOM"
CHAT_MESSAGE = "CHAT_MESSAGE"
OFFER = "OFFER"
ANSWER = "ANSWER"
ICE_CANDIDATE = "ICE_CANDIDATE"

CLIENT_EVENTS = frozenset(
    {
        JOIN_ROOM,
        LEAVE_ROOM,
        MUTE_CHANGED,
        SPEAKING_CHANGED,
        MODERATE,
        END_ROOM,
        CHAT_MESSAGE,
        OFFER,
        ANSWER,
        ICE_CANDIDATE,
    }
)

# --------------------------------------------------------- server -> client
ROOM_STATE = "ROOM_STATE"
ROOM_JOINED = "ROOM_JOINED"
PARTICIPANT_JOINED = "PARTICIPANT_JOINED"
PARTICIPANT_LEFT = "PARTICIPANT_LEFT"
PARTICIPANT_UPDATED = "PARTICIPANT_UPDATED"
ROOM_LOCKED = "ROOM_LOCKED"
ROOM_UNLOCKED = "ROOM_UNLOCKED"
ROOM_ENDED = "ROOM_ENDED"
KICKED = "KICKED"
MESSAGE = "MESSAGE"
MESSAGE_HISTORY = "MESSAGE_HISTORY"
ERROR = "ERROR"

SIGNAL_EVENTS = frozenset({OFFER, ANSWER, ICE_CANDIDATE})


def frame(event: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"type": event, "payload": payload or {}}


def error(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return frame(ERROR, payload)


def room_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return frame(ROOM_STATE, state)


def room_joined(room: Dict[str, Any], me: Dict[str, Any], permissions: Dict[str, Any]):
    return frame(
        ROOM_JOINED,
        {
            "roomId": room["id"],
            "room": room,
            "userId": me["id"],
            "participant": me,
            "role": me["role"],
            "permissions": permissions,
        },
    )


def participant_joined(participant: Dict[str, Any], room: Dict[str, Any]):
    return frame(PARTICIPANT_JOINED, {"participant": participant, "room": room})


def participant_left(user_id: str, reason: str, room: Dict[str, Any]):
    return frame(PARTICIPANT_LEFT, {"userId": user_id, "reason": reason, "room": room})


def participant_updated(participant: Dict[str, Any]):
    return frame(PARTICIPANT_UPDATED, {"userId": participant["id"], "participant": participant})


def mute_changed(user_id: str, muted: bool, by: Optional[str] = None):
    return frame(MUTE_CHANGED, {"userId": user_id, "muted": bool(muted), "by": by})


def speaking_changed(user_id: str, speaking: bool):
    return frame(SPEAKING_CHANGED, {"userId": user_id, "speaking": bool(speaking)})


def room_locked(room: Dict[str, Any], by: Optional[str]):
    return frame(ROOM_LOCKED, {"roomId": room["id"], "locked": True, "room": room, "by": by})


def room_unlocked(room: Dict[str, Any], by: Optional[str]):
    return frame(ROOM_UNLOCKED, {"roomId": room["id"], "locked": False, "room": room, "by": by})


def room_ended(room_id: str, by: Optional[str], reason: str = "ended_by_owner"):
    return frame(ROOM_ENDED, {"roomId": room_id, "reason": reason, "by": by})


def kicked(room_id: str, user_id: str, by: Optional[str], cooldown: int):
    return frame(
        KICKED,
        {
            "roomId": room_id,
            "userId": user_id,
            "by": by,
            "reason": "removed_by_moderator",
            "cooldownSeconds": cooldown,
        },
    )


def message(msg: Dict[str, Any]):
    payload = {"message": msg}
    payload.update(msg)
    return frame(MESSAGE, payload)


def message_history(room_id: str, messages: List[Dict[str, Any]]):
    return frame(MESSAGE_HISTORY, {"roomId": room_id, "messages": messages})


def relay(event: str, from_user_id: str, to_user_id: str, body: Dict[str, Any]):
    payload = {"fromUserId": from_user_id, "toUserId": to_user_id}
    payload.update(body)
    return frame(event, payload)
