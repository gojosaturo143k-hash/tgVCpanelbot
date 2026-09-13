"""Read-only room endpoints. All mutations happen over the websocket."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from rooms.manager import room_manager, valid_room_id
from security.rate_limit import client_ip, http_limiter

bp = Blueprint("rooms", __name__)


def _limited():
    return not http_limiter.allow("read", client_ip(request))


@bp.get("/api/rooms")
def list_rooms():
    if _limited():
        return jsonify({"code": "RATE_LIMITED", "message": "Too many requests"}), 429
    rooms = [room.info_dict() for room in room_manager.all_rooms() if not room.ended]
    return jsonify({"rooms": rooms})


@bp.get("/api/rooms/<room_id>")
def get_room(room_id: str):
    if _limited():
        return jsonify({"code": "RATE_LIMITED", "message": "Too many requests"}), 429
    if not valid_room_id(room_id):
        return jsonify({"code": "INVALID_ROOM", "message": "Invalid room id"}), 400
    room = room_manager.get(room_id)
    if room is None:
        return jsonify({"code": "ROOM_NOT_FOUND", "message": "Voice room does not exist"}), 404
    with room.lock:
        info = room.info_dict()
        info["participants"] = [p.to_dict() for p in room.participants()]
    return jsonify(info)
