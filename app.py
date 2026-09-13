"""Aerogram voice chat backend - the one and only Flask application.

    python backend/app.py

Flask + Flask-Sock. The websocket carries signalling and control only; voice
audio flows peer-to-peer over WebRTC and never enters this process.
"""

from __future__ import annotations

import logging
import os
import sys

# Allow `python backend/app.py` from any working directory.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from flask import Flask, jsonify, request  # noqa: E402
from flask_sock import Sock  # noqa: E402

from config import config  # noqa: E402
from firebase.admin import init_firebase  # noqa: E402
from routes import register_blueprints  # noqa: E402
from security.origin import cors_headers  # noqa: E402
from websocket import voice  # noqa: E402
from websocket.heartbeat import start_heartbeat  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("voice.app")


def create_app(start_background: bool = True) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_HTTP_BODY_BYTES
    app.config["JSON_SORT_KEYS"] = False
    app.config["SOCK_SERVER_OPTIONS"] = {
        "ping_interval": config.WS_PING_INTERVAL,
        "max_message_size": config.MAX_WS_MESSAGE_BYTES,
    }

    sock = Sock(app)
    register_blueprints(app)
    voice.register(sock)          # the single websocket implementation

    init_firebase()

    # ----------------------------------------------------------------- CORS
    @app.after_request
    def apply_cors(response):  # noqa: ANN001
        for key, value in cors_headers(request.headers.get("Origin")).items():
            response.headers[key] = value
        return response

    @app.route("/api/<path:_rest>", methods=["OPTIONS"])
    def preflight(_rest):  # noqa: ANN001
        return ("", 204)

    # --------------------------------------------------------- JSON errors
    @app.errorhandler(400)
    def bad_request(_e):  # noqa: ANN001
        return jsonify({"code": "BAD_REQUEST", "message": "Malformed request"}), 400

    @app.errorhandler(404)
    def not_found(_e):  # noqa: ANN001
        return jsonify({"code": "NOT_FOUND", "message": "Not found"}), 404

    @app.errorhandler(405)
    def not_allowed(_e):  # noqa: ANN001
        return jsonify({"code": "METHOD_NOT_ALLOWED", "message": "Method not allowed"}), 405

    @app.errorhandler(413)
    def too_large(_e):  # noqa: ANN001
        return jsonify({"code": "PAYLOAD_TOO_LARGE", "message": "Request body is too large"}), 413

    @app.errorhandler(429)
    def rate_limited(_e):  # noqa: ANN001
        return jsonify({"code": "RATE_LIMITED", "message": "Too many requests"}), 429

    @app.errorhandler(Exception)
    def unhandled(exc):  # noqa: ANN001
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return (
                jsonify({"code": "HTTP_ERROR", "message": exc.name}),
                exc.code or 500,
            )
        # Log internally; never leak a stack trace to the client.
        log.exception("unhandled error")
        return jsonify({"code": "SERVER_ERROR", "message": "Internal server error"}), 500

    if start_background:
        start_heartbeat()

    return app


# Importable WSGI-style handle (tests use create_app directly). The heartbeat
# thread is started only by the real entry point below.
app = create_app(start_background=False)


def _startup_warnings() -> None:
    if config.AUTH_SECRET_IS_EPHEMERAL:
        log.warning("AUTH_SECRET is not set - using an ephemeral per-process secret")
    if config.DEV_MODE:
        log.warning("DEV_MODE=true - relaxed auth fallbacks are enabled, do not use in production")
    if not config.firebase_configured():
        log.warning("Firebase is not configured - set FIREBASE_* environment variables")
    if not config.telegram_configured():
        log.warning("TELEGRAM_BOT_TOKEN is not set - Telegram identity is unavailable")
    if not config.FRONTEND_ORIGIN and not config.DEV_MODE:
        log.warning("FRONTEND_ORIGIN is not set - browser requests may be blocked by CORS")


if __name__ == "__main__":
    _startup_warnings()
    start_heartbeat()
    log.info("starting %s: %s", config.SERVICE_NAME, config.summary())
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        threaded=True,
        debug=False,
        use_reloader=False,
    )
