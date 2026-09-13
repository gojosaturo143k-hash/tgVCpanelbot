"""Single source of configuration truth.

Everything is environment driven. No secret is ever hardcoded, defaulted to a
real value, or logged.
"""

from __future__ import annotations

import os
import secrets
from typing import List, Set, Tuple


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _csv(name: str) -> List[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _id_set(name: str) -> Set[str]:
    return {str(v) for v in _csv(name)}


class Config:
    """Immutable-ish runtime configuration."""

    # ----------------------------------------------------------- server ---
    PORT: int = _int("PORT", 10000)
    HOST: str = "0.0.0.0"
    DEV_MODE: bool = _bool("DEV_MODE", False)
    SERVICE_NAME: str = "voice-chat-backend"

    # ------------------------------------------------------------- auth ---
    AUTH_SECRET: str = os.getenv("AUTH_SECRET", "").strip()
    AUTH_SECRET_IS_EPHEMERAL: bool = False
    if not AUTH_SECRET:
        AUTH_SECRET = secrets.token_urlsafe(48)
        AUTH_SECRET_IS_EPHEMERAL = True

    SESSION_TTL_SECONDS: int = _int("SESSION_TTL_SECONDS", 3600)  # 1 hour
    TICKET_TTL_SECONDS: int = _int("TICKET_TTL_SECONDS", 75)      # 60-90s window

    # ---------------------------------------------------------- firebase ---
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "").strip()
    FIREBASE_CLIENT_EMAIL: str = os.getenv("FIREBASE_CLIENT_EMAIL", "").strip()
    FIREBASE_PRIVATE_KEY: str = os.getenv("FIREBASE_PRIVATE_KEY", "")
    FIREBASE_SERVICE_ACCOUNT_JSON: str = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    GOOGLE_APPLICATION_CREDENTIALS: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

    # ---------------------------------------------------------- telegram ---
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_INIT_DATA_TTL: int = _int("TELEGRAM_INIT_DATA_TTL", 86400)
    TELEGRAM_REQUIRED: bool = _bool("TELEGRAM_REQUIRED", False)

    # Server side role assignment. Telegram user ids, never browser claims.
    OWNER_TELEGRAM_IDS: Set[str] = _id_set("OWNER_TELEGRAM_IDS")
    MODERATOR_TELEGRAM_IDS: Set[str] = _id_set("MODERATOR_TELEGRAM_IDS")

    # --------------------------------------------------------------- cors ---
    FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "")

    LOCAL_ORIGINS: Tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    )

    @classmethod
    def allowed_origins(cls) -> List[str]:
        origins = {o.strip().rstrip("/") for o in cls.FRONTEND_ORIGIN.split(",") if o.strip()}
        origins.update(cls.LOCAL_ORIGINS)
        # Telegram Mini Apps are served from these origins.
        origins.update({"https://web.telegram.org", "https://telegram.org"})
        return sorted(origins)

    # -------------------------------------------------------------- rooms ---
    MAX_PARTICIPANTS: int = _int("MAX_PARTICIPANTS", 20)
    DEFAULT_ROOM_ID: str = os.getenv("DEFAULT_ROOM_ID", "gossip-main")
    DEFAULT_ROOM_TITLE: str = os.getenv("DEFAULT_ROOM_TITLE", "Gossip")
    DEFAULT_ROOM_EMOJI: str = os.getenv("DEFAULT_ROOM_EMOJI", "\U0001f4ac")
    ALLOW_ROOM_AUTOCREATE: bool = _bool("ALLOW_ROOM_AUTOCREATE", False)
    EMPTY_ROOM_TTL_SECONDS: int = _int("EMPTY_ROOM_TTL_SECONDS", 600)
    KICK_COOLDOWN_SECONDS: int = _int("KICK_COOLDOWN_SECONDS", 60)

    # --------------------------------------------------------------- chat ---
    CHAT_ENABLED: bool = _bool("CHAT_ENABLED", True)
    MAX_CHAT_LENGTH: int = _int("MAX_CHAT_LENGTH", 500)
    MAX_CHAT_HISTORY: int = _int("MAX_CHAT_HISTORY", 50)

    # ---------------------------------------------------------- websocket ---
    WS_PING_INTERVAL: int = _int("WS_PING_INTERVAL", 25)
    WS_RECEIVE_TIMEOUT: int = _int("WS_RECEIVE_TIMEOUT", 5)
    WS_STALE_AFTER_SECONDS: int = _int("WS_STALE_AFTER_SECONDS", 90)
    HEARTBEAT_SWEEP_SECONDS: int = _int("HEARTBEAT_SWEEP_SECONDS", 10)
    MAX_WS_MESSAGE_BYTES: int = _int("MAX_WS_MESSAGE_BYTES", 65536)
    MAX_SDP_BYTES: int = _int("MAX_SDP_BYTES", 49152)
    MAX_ICE_BYTES: int = _int("MAX_ICE_BYTES", 8192)
    MAX_HTTP_BODY_BYTES: int = _int("MAX_HTTP_BODY_BYTES", 65536)

    # ------------------------------------------------------ abuse control ---
    MAX_CONNECTIONS_PER_IP: int = _int("MAX_CONNECTIONS_PER_IP", 8)
    MAX_CONNECTIONS_PER_USER: int = _int("MAX_CONNECTIONS_PER_USER", 3)

    # event -> (bucket capacity, refill tokens/second)
    EVENT_RATE_LIMITS = {
        "JOIN_ROOM": (5, 0.5),
        "LEAVE_ROOM": (10, 1.0),
        "END_ROOM": (3, 0.2),
        "MUTE_CHANGED": (12, 3.0),
        "SPEAKING_CHANGED": (20, 8.0),
        "MODERATE": (15, 2.0),
        "CHAT_MESSAGE": (8, 1.0),
        "OFFER": (40, 8.0),
        "ANSWER": (40, 8.0),
        "ICE_CANDIDATE": (160, 40.0),
        "__global__": (240, 60.0),
    }
    HTTP_RATE_LIMITS = {
        "session": (20, 0.4),
        "ticket": (40, 1.0),
        "read": (120, 4.0),
    }
    MAX_PROTOCOL_VIOLATIONS: int = _int("MAX_PROTOCOL_VIOLATIONS", 40)

    # ------------------------------------------------------------ helpers ---
    @classmethod
    def firebase_configured(cls) -> bool:
        return bool(
            (cls.FIREBASE_PROJECT_ID and cls.FIREBASE_CLIENT_EMAIL and cls.FIREBASE_PRIVATE_KEY)
            or cls.FIREBASE_SERVICE_ACCOUNT_JSON
            or cls.GOOGLE_APPLICATION_CREDENTIALS
        )

    @classmethod
    def telegram_configured(cls) -> bool:
        return bool(cls.TELEGRAM_BOT_TOKEN)

    @classmethod
    def summary(cls) -> dict:
        """Safe, secret-free snapshot for logs and /health."""
        return {
            "service": cls.SERVICE_NAME,
            "devMode": cls.DEV_MODE,
            "port": cls.PORT,
            "firebaseConfigured": cls.firebase_configured(),
            "telegramConfigured": cls.telegram_configured(),
            "telegramRequired": cls.TELEGRAM_REQUIRED,
            "maxParticipants": cls.MAX_PARTICIPANTS,
            "chatEnabled": cls.CHAT_ENABLED,
            "defaultRoom": cls.DEFAULT_ROOM_ID,
        }


config = Config()
