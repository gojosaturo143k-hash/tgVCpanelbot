"""Heartbeat and stale-connection reaper.

simple-websocket sends protocol pings every ``WS_PING_INTERVAL`` seconds (set
on the Sock extension). This sweeper is the second line of defence: sockets
that died without the read loop noticing are cleaned up so the participant list
never keeps ghosts.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict

from config import config
from rooms.manager import room_manager

from .voice import cleanup, registry_snapshot

log = logging.getLogger("voice.heartbeat")

_started = False
_start_lock = threading.Lock()
_stats: Dict[str, int] = {"sweeps": 0, "reaped": 0}


def sweep_once() -> int:
    """Reap dead/stale connections. Returns how many were removed."""
    now = time.time()
    reaped = 0
    for conn in registry_snapshot():
        stale = (now - conn.last_seen) > config.WS_STALE_AFTER_SECONDS
        if conn.alive and not stale:
            continue
        log.info("reaping socket user=%s room=%s stale=%s", conn.user_id, conn.room_id, stale)
        try:
            cleanup(conn, "timeout")
        except Exception:  # noqa: BLE001
            log.exception("cleanup failed")
        reaped += 1
    room_manager.drop_empty()
    _stats["sweeps"] += 1
    _stats["reaped"] += reaped
    return reaped


def stats() -> Dict[str, int]:
    return dict(_stats)


def _loop() -> None:
    while True:
        time.sleep(config.HEARTBEAT_SWEEP_SECONDS)
        try:
            sweep_once()
        except Exception:  # noqa: BLE001
            log.exception("heartbeat sweep failed")


def start_heartbeat() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="ws-heartbeat", daemon=True).start()
    log.info("heartbeat sweeper started (every %ss)", config.HEARTBEAT_SWEEP_SECONDS)
