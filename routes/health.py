"""Health and diagnostics. Never exposes secrets."""

from __future__ import annotations

import time

from flask import Blueprint, jsonify

from config import config
from firebase import admin as firebase_admin_wrapper
from rooms.manager import room_manager

bp = Blueprint("health", __name__)
_STARTED_AT = time.time()


@bp.get("/")
def root():
    return jsonify({"status": "ok", "service": config.SERVICE_NAME})


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@bp.get("/api/health")
def detailed_health():
    from websocket.heartbeat import stats as heartbeat_stats
    from websocket.voice import connection_count

    stats = room_manager.stats()
    return jsonify(
        {
            "status": "ok",
            "service": config.SERVICE_NAME,
            "uptimeSeconds": int(time.time() - _STARTED_AT),
            "connections": connection_count(),
            "heartbeat": heartbeat_stats(),
            "firebase": firebase_admin_wrapper.state(),
            **stats,
            **config.summary(),
        }
    )
