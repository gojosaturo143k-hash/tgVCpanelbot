"""/ws/voice/<room_id> - the single websocket implementation.

Signalling and control only. Audio travels browser-to-browser over WebRTC and
never enters this process; nothing is recorded or stored.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from flask import request

from auth.identity import (
    ROLE_MODERATOR,
    ROLE_OWNER,
    ROLE_PARTICIPANT,
    AppUser,
    can_claim_ownerless_room,
    identity_store,
    resolve_role,
)
from auth.tickets import TicketError, redeem_ticket
from config import config
from rooms.manager import room_manager, valid_room_id
from rooms.participant import Participant
from security.origin import is_allowed
from security.rate_limit import EventRateLimiter, client_ip, connection_counter
from security.validation import (
    ValidationError,
    parse_frame,
    require_bool,
    require_user_id,
    validate_action,
    validate_chat_text,
    validate_ice,
    validate_sdp,
)

from . import protocol as P

log = logging.getLogger("voice.ws")

_registry_lock = threading.Lock()
_connections: Dict[str, "Connection"] = {}


# ---------------------------------------------------------------- registry
def registry_snapshot() -> List["Connection"]:
    with _registry_lock:
        return list(_connections.values())


def connection_count() -> int:
    with _registry_lock:
        return len(_connections)


class Connection:
    """One authenticated websocket client."""

    def __init__(self, ws, room_id: str, user: AppUser, ip: str) -> None:
        self.cid = uuid.uuid4().hex
        self.ws = ws
        self.room_id = room_id
        self.user = user
        self.ip = ip
        self.joined = False
        self.closing = False
        self.created_at = time.time()
        self.last_seen = time.time()
        self.limiter = EventRateLimiter()
        self._send_lock = threading.Lock()
        self._last_speaking: Optional[bool] = None

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def alive(self) -> bool:
        if self.closing:
            return False
        return bool(getattr(self.ws, "connected", True))

    def send(self, payload: Dict[str, Any]) -> bool:
        if self.closing:
            return False
        try:
            raw = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError):
            log.warning("dropping unserialisable frame %s", payload.get("type"))
            return False
        try:
            with self._send_lock:
                self.ws.send(raw)
            return True
        except Exception:  # noqa: BLE001 - peer vanished
            self.closing = True
            return False

    def send_error(self, code: str, message: str, **extra: Any) -> None:
        self.send(P.error(code, message, **extra))

    def close(self) -> None:
        self.closing = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------- helpers
def _broadcast(room, frame: Dict[str, Any], exclude: Optional[List[str]] = None) -> None:
    with room.lock:
        targets = room.connections(exclude=exclude)
    for target in targets:
        target.send(frame)


def _membership(conn: Connection) -> Tuple[Optional[Any], Optional[Participant]]:
    room = room_manager.get(conn.room_id)
    if room is None or room.ended:
        conn.send_error("ROOM_NOT_FOUND", "Voice room does not exist")
        return None, None
    with room.lock:
        me = room.get(conn.user_id)
        mine = me is not None and me.connection is conn
    if not conn.joined or not mine:
        conn.send_error("NOT_IN_ROOM", "Join the room first")
        return None, None
    return room, me


# -------------------------------------------------------------- JOIN_ROOM
def _handle_join(conn: Connection, payload: Dict[str, Any]) -> None:
    requested = payload.get("roomId")
    if requested is not None and requested != conn.room_id:
        conn.send_error("ROOM_MISMATCH", "roomId does not match this socket")
        return

    room = room_manager.get(conn.room_id)
    if room is None or room.ended:
        conn.send_error("ROOM_NOT_FOUND", "Voice room does not exist")
        return

    if conn.joined:
        # Idempotent re-join: just resend the state.
        with room.lock:
            state = room.state_for(conn.user_id)
            history = room.history()
        conn.send(P.room_state(state))
        conn.send(P.message_history(room.id, history))
        return

    displaced = None
    with room.lock:
        cooldown = room.cooldown_remaining(conn.user_id)
        if cooldown:
            conn.send_error(
                "KICK_COOLDOWN", "You were removed from this room", retryAfter=cooldown
            )
            return

        existing = room.get(conn.user_id)
        if existing is not None and existing.connection is not None and existing.connection is not conn:
            # Duplicate session - newest socket wins, the old one is retired.
            displaced = existing.connection

        if existing is None:
            if room.locked:
                conn.send_error("ROOM_LOCKED", "Voice room is locked")
                return
            if room.is_full():
                conn.send_error("ROOM_FULL", "Voice room is full")
                return

        role = resolve_role(conn.user, room.owner_id)
        if role == ROLE_OWNER:
            # A configured owner takes ownership of the room record.
            room.adopt_owner(conn.user_id)
        elif room.owner_id is None and can_claim_ownerless_room(conn.user):
            if room.adopt_owner(conn.user_id):
                role = ROLE_OWNER

        participant = existing or Participant(user=conn.user, role=role, muted=True)
        participant.user = conn.user  # refresh profile (name/photo may change)
        participant.role = role
        participant.speaking = False
        participant.joined = True
        participant.connection = conn
        is_rejoin = existing is not None
        room.add(participant)

        state = room.state_for(conn.user_id)
        info = room.info_dict()
        me = participant.to_dict()
        permissions = participant.permissions()
        history = room.history()

    if displaced is not None:
        displaced.joined = False
        displaced.send_error("DUPLICATE_SESSION", "Joined from another tab or device")
        displaced.close()

    conn.joined = True
    conn.send(P.room_state(state))
    conn.send(P.room_joined(info, me, permissions))
    conn.send(P.message_history(room.id, history))
    _broadcast(room, P.participant_joined(me, info), exclude=[conn.user_id])
    if is_rejoin:
        _broadcast(room, P.participant_updated(me), exclude=[conn.user_id])
    log.info("join room=%s user=%s role=%s", room.id, conn.user_id, me["role"])


def leave_room(conn: Connection, reason: str = "left") -> None:
    """Remove the participant from the room. Idempotent."""
    room = room_manager.get(conn.room_id)
    conn.joined = False
    if room is None:
        return
    info = None
    with room.lock:
        participant = room.get(conn.user_id)
        if participant is not None and participant.connection is conn:
            room.remove(conn.user_id)
            info = room.info_dict()
    if info is not None:
        _broadcast(room, P.participant_left(conn.user_id, reason, info))
        log.info("leave room=%s user=%s reason=%s", room.id, conn.user_id, reason)
    room_manager.drop_empty()


# ----------------------------------------------------------- participant state
def _handle_mute(conn: Connection, payload: Dict[str, Any]) -> None:
    room, _ = _membership(conn)
    if room is None:
        return
    target = payload.get("userId", conn.user_id)
    if target != conn.user_id:
        conn.send_error("FORBIDDEN", "You can only change your own microphone")
        return
    muted = require_bool(payload, "muted")

    with room.lock:
        me = room.get(conn.user_id)
        if me is None:
            return
        if me.restricted and not muted:
            snapshot = me.to_dict()
            conn.send_error("RESTRICTED", "You are not allowed to speak in this room")
            conn.send(P.participant_updated(snapshot))
            return
        me.muted = muted
        if muted:
            me.speaking = False
        snapshot = me.to_dict()

    _broadcast(room, P.mute_changed(conn.user_id, muted, by=conn.user_id))
    _broadcast(room, P.participant_updated(snapshot))


def _handle_speaking(conn: Connection, payload: Dict[str, Any]) -> None:
    room, _ = _membership(conn)
    if room is None:
        return
    target = payload.get("userId", conn.user_id)
    if target != conn.user_id:
        conn.send_error("FORBIDDEN", "You can only report your own speaking state")
        return
    speaking = require_bool(payload, "speaking")

    with room.lock:
        me = room.get(conn.user_id)
        if me is None:
            return
        if me.muted or me.restricted:
            speaking = False
        if me.speaking == speaking and conn._last_speaking == speaking:
            return
        me.speaking = speaking
    conn._last_speaking = speaking
    _broadcast(room, P.speaking_changed(conn.user_id, speaking))


# ---------------------------------------------------------------- moderation
def _handle_moderate(conn: Connection, payload: Dict[str, Any]) -> None:
    room, _ = _membership(conn)
    if room is None:
        return
    action = validate_action(payload)

    if action in ("lock", "unlock"):
        _handle_lock(conn, room, action == "lock")
        return

    target_id = require_user_id(payload, "targetUserId", "userId")
    target_conn = None
    snapshot = None
    info = None

    with room.lock:
        actor = room.get(conn.user_id)
        target = room.get(target_id)
        if actor is None:
            return
        if not actor.can_moderate:
            conn.send_error("FORBIDDEN", "You are not allowed to moderate this room")
            return
        if target is None:
            conn.send_error("TARGET_NOT_FOUND", "That participant is not in the room")
            return
        if target.id == actor.id:
            conn.send_error("INVALID_TARGET", "You cannot moderate yourself")
            return
        if target.role == ROLE_OWNER:
            conn.send_error("FORBIDDEN", "The room owner cannot be moderated")
            return
        if actor.role == ROLE_MODERATOR and target.role == ROLE_MODERATOR:
            conn.send_error("FORBIDDEN", "Moderators cannot moderate each other")
            return

        if action == "mute":
            target.muted = True
            target.speaking = False
        elif action == "unmute" or action == "allow":
            target.restricted = False
        elif action == "restrict":
            target.restricted = True
            target.muted = True
            target.speaking = False
        elif action in ("remove", "kick"):
            room.set_cooldown(target.id, config.KICK_COOLDOWN_SECONDS)
            target_conn = target.connection
            room.remove(target.id)
            info = room.info_dict()

        if action not in ("remove", "kick"):
            snapshot = target.to_dict()

    if action in ("remove", "kick"):
        if target_conn is not None:
            target_conn.joined = False
            target_conn.send(
                P.kicked(room.id, target_id, conn.user_id, config.KICK_COOLDOWN_SECONDS)
            )
            time.sleep(0.05)
            target_conn.close()
        if info is not None:
            _broadcast(room, P.participant_left(target_id, "removed", info))
        log.info("kick room=%s target=%s by=%s", room.id, target_id, conn.user_id)
        return

    if snapshot is not None:
        _broadcast(room, P.participant_updated(snapshot))
        if action in ("mute", "restrict"):
            _broadcast(room, P.mute_changed(target_id, True, by=conn.user_id))
            _broadcast(room, P.speaking_changed(target_id, False))


def _handle_lock(conn: Connection, room, locked: bool) -> None:
    with room.lock:
        actor = room.get(conn.user_id)
        if actor is None or actor.role != ROLE_OWNER:
            conn.send_error("FORBIDDEN", "Only the room owner can lock or unlock the room")
            return
        room.set_locked(locked)
        info = room.info_dict()
    # Locking never disconnects anybody who is already inside.
    _broadcast(room, P.room_locked(info, conn.user_id) if locked else P.room_unlocked(info, conn.user_id))
    log.info("room %s %s by %s", room.id, "locked" if locked else "unlocked", conn.user_id)


def _handle_end_room(conn: Connection) -> None:
    room, _ = _membership(conn)
    if room is None:
        return
    with room.lock:
        actor = room.get(conn.user_id)
        if actor is None or actor.role != ROLE_OWNER:
            conn.send_error("FORBIDDEN", "Only the room owner can end the room")
            return
        room.end()
        targets = room.connections()

    frame = P.room_ended(room.id, conn.user_id)
    for target in targets:
        target.joined = False
        target.send(frame)
    time.sleep(0.05)
    for target in targets:
        target.close()
    room_manager.reset(room.id)
    log.info("room ended room=%s by=%s", room.id, conn.user_id)


# --------------------------------------------------------------------- chat
def _handle_chat(conn: Connection, payload: Dict[str, Any]) -> None:
    room, _ = _membership(conn)
    if room is None:
        return
    text = validate_chat_text(payload)
    with room.lock:
        sender = room.get(conn.user_id)
        if sender is None:
            return
        if sender.restricted:
            conn.send_error("RESTRICTED", "You are restricted from chatting")
            return
        msg = room.add_message(sender, text)
    _broadcast(room, P.message(msg))


# -------------------------------------------------------------- WebRTC relay
def _handle_signal(conn: Connection, event: str, payload: Dict[str, Any]) -> None:
    room, _ = _membership(conn)
    if room is None:
        return

    sender = payload.get("fromUserId", conn.user_id)
    if sender != conn.user_id:
        conn.send_error("FORBIDDEN", "fromUserId must be your own user id")
        return
    to_user = require_user_id(payload, "toUserId")
    if to_user == conn.user_id:
        conn.send_error("INVALID_TARGET", "Cannot signal yourself")
        return

    body = (
        {"candidate": validate_ice(payload)}
        if event == P.ICE_CANDIDATE
        else {"sdp": validate_sdp(payload, event)}
    )

    # The target is only ever looked up inside this socket's own room, so
    # cross-room signalling is structurally impossible.
    with room.lock:
        target = room.get(to_user)
        target_conn = target.connection if target else None
    if target_conn is None or not target_conn.joined:
        conn.send_error("TARGET_NOT_FOUND", "Target participant is not in this room")
        return
    target_conn.send(P.relay(event, conn.user_id, to_user, body))


# ------------------------------------------------------------------- router
_HANDLERS = {
    P.JOIN_ROOM: lambda c, p: _handle_join(c, p),
    P.LEAVE_ROOM: lambda c, p: leave_room(c, "left"),
    P.MUTE_CHANGED: lambda c, p: _handle_mute(c, p),
    P.SPEAKING_CHANGED: lambda c, p: _handle_speaking(c, p),
    P.MODERATE: lambda c, p: _handle_moderate(c, p),
    P.END_ROOM: lambda c, p: _handle_end_room(c),
    P.CHAT_MESSAGE: lambda c, p: _handle_chat(c, p),
}


def dispatch(conn: Connection, event: str, payload: Dict[str, Any]) -> None:
    if event in P.SIGNAL_EVENTS:
        _handle_signal(conn, event, payload)
        return
    handler = _HANDLERS.get(event)
    if handler is None:
        conn.send_error("UNKNOWN_EVENT", f"Unsupported event '{event}'")
        return
    handler(conn, payload)


# --------------------------------------------------------------- handshake
def _reject(ws, code: str, message: str) -> None:
    try:
        ws.send(json.dumps(P.error(code, message)))
        ws.close()
    except Exception:  # noqa: BLE001
        pass


def _authenticate(ws, room_id: str) -> Optional[AppUser]:
    token = request.args.get("token", "")
    if not token:
        _reject(ws, "UNAUTHORIZED", "A websocket ticket is required")
        return None
    try:
        payload = redeem_ticket(token, room_id)
    except TicketError as err:
        _reject(ws, "UNAUTHORIZED", err.message)
        return None

    user = identity_store.get(payload["sub"])
    if user is None:
        _reject(ws, "UNAUTHORIZED", "Unknown identity, please re-authenticate")
        return None
    return user


def register(sock) -> None:
    """Register the single websocket route on the Flask-Sock instance."""

    @sock.route("/ws/voice/<room_id>")
    def voice_socket(ws, room_id):  # noqa: ANN001
        if not is_allowed(request.headers.get("Origin")):
            _reject(ws, "ORIGIN_NOT_ALLOWED", "Origin is not allowed")
            return
        if not valid_room_id(room_id):
            _reject(ws, "INVALID_ROOM", "Invalid room id")
            return
        if room_manager.get(room_id) is None:
            _reject(ws, "ROOM_NOT_FOUND", "Voice room does not exist")
            return

        user = _authenticate(ws, room_id)
        if user is None:
            return

        ip = client_ip(request)
        ok, why = connection_counter.acquire(ip, user.id)
        if not ok:
            _reject(ws, "RATE_LIMITED", why)
            return

        conn = Connection(ws, room_id, user, ip)
        with _registry_lock:
            _connections[conn.cid] = conn
        log.info("socket open room=%s user=%s", room_id, user.id)

        try:
            read_loop(conn)
        except Exception as exc:  # noqa: BLE001 - a dead socket must not kill the worker
            log.info("socket error user=%s (%s)", conn.user_id, type(exc).__name__)
        finally:
            cleanup(conn, "disconnected")


def read_loop(conn: Connection) -> None:
    while not conn.closing:
        raw = conn.ws.receive(timeout=config.WS_RECEIVE_TIMEOUT)
        if raw is None:
            # idle tick: protocol level ping/pong keeps the socket honest
            if not conn.alive:
                return
            conn.last_seen = time.time()
            continue
        conn.last_seen = time.time()

        try:
            event, payload = parse_frame(raw)
        except ValidationError as err:
            conn.send_error(err.code, err.message)
            if conn.limiter.note_violation() and conn.limiter.exhausted:
                conn.send_error("RATE_LIMITED", "Too many invalid messages")
                return
            continue

        if event not in P.CLIENT_EVENTS:
            conn.send_error("UNKNOWN_EVENT", f"Unsupported event '{event}'")
            if conn.limiter.note_violation() and conn.limiter.exhausted:
                return
            continue

        if not conn.limiter.allow(event):
            conn.send_error("RATE_LIMITED", "Too many requests")
            if conn.limiter.violations > config.MAX_PROTOCOL_VIOLATIONS * 2:
                return
            continue

        try:
            dispatch(conn, event, payload)
        except ValidationError as err:
            conn.send_error(err.code, err.message)
        except Exception:  # noqa: BLE001
            log.exception("handler failed event=%s", event)
            conn.send_error("SERVER_ERROR", "Could not process that message")


def cleanup(conn: Connection, reason: str) -> None:
    """Idempotent teardown: no ghost participants, no leaked counters."""
    with _registry_lock:
        removed = _connections.pop(conn.cid, None)
    try:
        leave_room(conn, reason)
    finally:
        conn.closing = True
        if removed is not None:
            connection_counter.release(conn.ip, conn.user_id)
        try:
            conn.ws.close()
        except Exception:  # noqa: BLE001
            pass
