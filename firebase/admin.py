"""Firebase Admin SDK wrapper.

Responsibilities:
  * initialise the Admin SDK from environment variables (never from source)
  * verify Firebase ID tokens (anonymous auth included)

Credentials are read from, in order of precedence:
  1. FIREBASE_SERVICE_ACCOUNT_JSON   (raw JSON string)
  2. FIREBASE_PROJECT_ID + FIREBASE_CLIENT_EMAIL + FIREBASE_PRIVATE_KEY
  3. GOOGLE_APPLICATION_CREDENTIALS  (path, handled by the SDK itself)

Nothing about the credentials is ever logged or returned to a client.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

from config import config

log = logging.getLogger("voice.firebase")

_lock = threading.Lock()
_app = None
_state = "uninitialised"  # uninitialised | ready | unavailable | disabled


class FirebaseError(Exception):
    """Raised when a Firebase ID token cannot be verified."""

    def __init__(self, code: str = "FIREBASE_AUTH_FAILED", message: str = "Invalid Firebase token"):
        super().__init__(message)
        self.code = code
        self.message = message


def _credentials():
    from firebase_admin import credentials

    if config.FIREBASE_SERVICE_ACCOUNT_JSON:
        try:
            info = json.loads(config.FIREBASE_SERVICE_ACCOUNT_JSON)
        except ValueError as exc:
            raise FirebaseError("FIREBASE_CONFIG", "Service account JSON is malformed") from exc
        return credentials.Certificate(info)

    if config.FIREBASE_PROJECT_ID and config.FIREBASE_CLIENT_EMAIL and config.FIREBASE_PRIVATE_KEY:
        # Render stores multi-line keys with escaped newlines.
        private_key = config.FIREBASE_PRIVATE_KEY.replace("\\n", "\n").strip()
        info = {
            "type": "service_account",
            "project_id": config.FIREBASE_PROJECT_ID,
            "client_email": config.FIREBASE_CLIENT_EMAIL,
            "private_key": private_key,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        return credentials.Certificate(info)

    if config.GOOGLE_APPLICATION_CREDENTIALS:
        return credentials.ApplicationDefault()

    return None


def init_firebase() -> str:
    """Initialise the SDK once. Returns the resulting state string."""
    global _app, _state

    with _lock:
        if _state in ("ready", "disabled", "unavailable"):
            return _state

        if not config.firebase_configured():
            _state = "disabled"
            if config.DEV_MODE:
                log.warning("Firebase not configured - DEV_MODE identity fallback is active")
            else:
                log.error("Firebase not configured - authentication requests will be rejected")
            return _state

        try:
            import firebase_admin
        except ImportError:
            _state = "unavailable"
            log.error("firebase-admin is not installed - run pip install -r requirements.txt")
            return _state

        try:
            creds = _credentials()
            options = {"projectId": config.FIREBASE_PROJECT_ID} if config.FIREBASE_PROJECT_ID else None
            if firebase_admin._apps:  # pragma: no cover - re-init in tests
                _app = firebase_admin.get_app()
            elif creds is not None:
                _app = firebase_admin.initialize_app(creds, options)
            else:
                _app = firebase_admin.initialize_app(options=options)
            _state = "ready"
            log.info("Firebase Admin initialised (project configured: %s)", bool(config.FIREBASE_PROJECT_ID))
        except Exception as exc:  # noqa: BLE001 - never leak credential details
            _state = "unavailable"
            log.error("Firebase Admin initialisation failed: %s", type(exc).__name__)
        return _state


def state() -> str:
    return _state


def is_ready() -> bool:
    return _state == "ready"


def verify_id_token(id_token: str) -> Dict[str, Any]:
    """Verify a Firebase ID token and return a normalised claim set."""
    if not isinstance(id_token, str) or not id_token.strip():
        raise FirebaseError("FIREBASE_TOKEN_MISSING", "Firebase ID token is required")
    if len(id_token) > 8192:
        raise FirebaseError("FIREBASE_TOKEN_INVALID", "Firebase ID token is too large")

    if _state != "ready":
        init_firebase()

    if _state != "ready":
        # DEV_MODE escape hatch so the stack is runnable without Firebase keys.
        if config.DEV_MODE and id_token.startswith("dev:"):
            uid = id_token[4:].strip()[:64] or "devuser"
            log.warning("DEV_MODE: accepting unverified development identity")
            return {
                "uid": f"dev_{uid}",
                "provider": "dev",
                "anonymous": True,
                "verified": False,
            }
        raise FirebaseError("FIREBASE_UNAVAILABLE", "Authentication service is not configured")

    from firebase_admin import auth as fb_auth

    try:
        decoded = fb_auth.verify_id_token(id_token, check_revoked=False)
    except Exception as exc:  # noqa: BLE001 - map every SDK error to one code
        name = type(exc).__name__
        if "Expired" in name:
            raise FirebaseError("FIREBASE_TOKEN_EXPIRED", "Firebase token has expired") from exc
        if "Revoked" in name:
            raise FirebaseError("FIREBASE_TOKEN_REVOKED", "Firebase token was revoked") from exc
        raise FirebaseError("FIREBASE_TOKEN_INVALID", "Firebase token is not valid") from exc

    uid = decoded.get("uid") or decoded.get("sub")
    if not uid:
        raise FirebaseError("FIREBASE_TOKEN_INVALID", "Firebase token has no subject")

    provider = (decoded.get("firebase") or {}).get("sign_in_provider", "unknown")
    return {
        "uid": str(uid),
        "provider": provider,
        "anonymous": provider == "anonymous",
        "verified": True,
    }


def optional_verify(id_token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not id_token:
        return None
    try:
        return verify_id_token(id_token)
    except FirebaseError:
        return None
