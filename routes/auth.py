"""Authentication endpoints.

    POST /api/auth/session   Firebase ID token (+ Telegram initData) -> session
    POST /api/auth/ticket    session -> single-use, ~75s websocket ticket
    GET  /api/auth/me        identity behind a session token

Trust model
-----------
* The Firebase ID token is verified with the Admin SDK.
* Telegram initData is verified with HMAC-SHA256 against TELEGRAM_BOT_TOKEN.
* Display name / username / photo / Telegram id are taken **only** from those
  verified sources. Anything else in the body is ignored.
* Roles are computed server side; a client can never request one.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from auth.identity import identity_store, resolve_role
from auth.tickets import TicketError, issue_session, issue_ticket, verify_session
from config import config
from firebase.admin import FirebaseError, verify_id_token
from rooms.manager import room_manager, valid_room_id
from security.rate_limit import client_ip, http_limiter
from security.validation import sanitize_text
from telegram.identity import build_profile
from telegram.init_data import TelegramAuthError, verify_init_data

log = logging.getLogger("voice.auth")

bp = Blueprint("auth", __name__)


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _fail(code: str, message: str, status: int):
    return jsonify({"code": code, "message": message}), status


def _bearer() -> str:
    header = request.headers.get("Authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


@bp.post("/api/auth/session")
def create_session():
    if not http_limiter.allow("session", client_ip(request)):
        return _fail("RATE_LIMITED", "Too many requests", 429)

    body = _body()
    room_id = body.get("roomId") or config.DEFAULT_ROOM_ID
    if not valid_room_id(room_id):
        return _fail("INVALID_ROOM", "Invalid room id", 400)
    if room_manager.get(room_id) is None:
        return _fail("ROOM_NOT_FOUND", "Voice room does not exist", 404)

    # 1. Firebase identity (anonymous auth included) ------------------------
    firebase_token = body.get("firebaseIdToken") or body.get("idToken") or _bearer()
    try:
        firebase_claims = verify_id_token(firebase_token)
    except FirebaseError as err:
        return _fail(err.code, err.message, 401)

    # 2. Telegram identity, when the app was opened from Telegram ----------
    telegram_profile = None
    init_data = body.get("initData") or body.get("telegramInitData")
    if init_data:
        if not config.telegram_configured():
            return _fail("TELEGRAM_NOT_CONFIGURED", "Telegram verification is not configured", 503)
        try:
            telegram_profile = build_profile(verify_init_data(init_data))
        except TelegramAuthError as err:
            return _fail(err.code, err.message, 401)
    elif config.TELEGRAM_REQUIRED:
        return _fail(
            "TELEGRAM_REQUIRED", "Open this voice chat from the Telegram group", 401
        )

    # 3. Link into one stable application identity -------------------------
    fallback_name = None
    if telegram_profile is None and config.DEV_MODE:
        # Dev-only convenience; ignored entirely when Telegram data is present.
        fallback_name = sanitize_text(body.get("displayName"), 32) or None

    user = identity_store.link(
        firebase_uid=firebase_claims["uid"],
        telegram=telegram_profile,
        fallback_name=fallback_name,
    )

    role = resolve_role(user, (room_manager.get(room_id).owner_id if room_manager.get(room_id) else None))
    session = issue_session(
        {
            "id": user.id,
            "firebase_uid": user.firebase_uid,
            "telegram_id": user.telegram_id,
            "display_name": user.display_name,
            "username": user.username,
            "photo_url": user.photo_url,
            "source": user.source,
        },
        room_id,
    )
    log.info("session issued user=%s source=%s room=%s", user.id, user.source, room_id)

    return jsonify(
        {
            "token": session["token"],
            "expiresIn": session["expiresIn"],
            "roomId": room_id,
            "user": {
                **user.to_public_dict(),
                "name": user.display_name,
                "role": role,
                "firebaseVerified": bool(firebase_claims.get("verified")),
                "anonymous": bool(firebase_claims.get("anonymous")),
            },
        }
    )


@bp.post("/api/auth/ticket")
def create_ticket():
    if not http_limiter.allow("ticket", client_ip(request)):
        return _fail("RATE_LIMITED", "Too many requests", 429)

    token = _bearer() or (_body().get("token") or "")
    if not token:
        return _fail("UNAUTHORIZED", "A session token is required", 401)
    try:
        payload = verify_session(token)
    except TicketError as err:
        return _fail("UNAUTHORIZED", err.message, 401)

    if identity_store.get(payload["sub"]) is None:
        return _fail("UNAUTHORIZED", "Unknown identity, please re-authenticate", 401)

    room_id = _body().get("roomId") or payload.get("room") or config.DEFAULT_ROOM_ID
    if not valid_room_id(room_id):
        return _fail("INVALID_ROOM", "Invalid room id", 400)
    if room_manager.get(room_id) is None:
        return _fail("ROOM_NOT_FOUND", "Voice room does not exist", 404)

    ticket = issue_ticket(payload, room_id)
    return jsonify(
        {
            "ticket": ticket["ticket"],
            "expiresIn": ticket["expiresIn"],
            "roomId": room_id,
            "userId": payload["sub"],
        }
    )


@bp.get("/api/auth/me")
def me():
    token = _bearer()
    if not token:
        return _fail("UNAUTHORIZED", "A session token is required", 401)
    try:
        payload = verify_session(token)
    except TicketError as err:
        return _fail("UNAUTHORIZED", err.message, 401)

    user = identity_store.get(payload["sub"])
    if user is None:
        return _fail("UNAUTHORIZED", "Unknown identity", 401)

    room = room_manager.get(payload.get("room") or config.DEFAULT_ROOM_ID)
    return jsonify(
        {
            **user.to_public_dict(),
            "name": user.display_name,
            "role": resolve_role(user, room.owner_id if room else None),
            "roomId": payload.get("room"),
        }
    )


@bp.get("/api/auth/config")
def public_config():
    """Public, secret-free hints for the frontend."""
    return jsonify(
        {
            "telegramRequired": config.TELEGRAM_REQUIRED,
            "telegramEnabled": config.telegram_configured(),
            "firebaseRequired": config.firebase_configured() or not config.DEV_MODE,
            "devMode": config.DEV_MODE,
            "defaultRoomId": config.DEFAULT_ROOM_ID,
            "maxParticipants": config.MAX_PARTICIPANTS,
            "chatEnabled": config.CHAT_ENABLED,
        }
    )
