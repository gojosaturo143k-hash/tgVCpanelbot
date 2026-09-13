"""Health, Firebase verification, Telegram verification, tickets, origins."""

from __future__ import annotations

import time
import unittest

from conftest import TEST_BOT_TOKEN, FakeFirebase, make_init_data  # noqa: E402

import routes.auth as auth_routes  # noqa: E402
from app import create_app  # noqa: E402
from auth.tickets import issue_session, issue_ticket, redeem_ticket, TicketError  # noqa: E402
from config import config  # noqa: E402
from telegram.init_data import TelegramAuthError, verify_init_data  # noqa: E402


class HealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = create_app(start_background=False).test_client()

    def test_root(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"status": "ok", "service": "voice-chat-backend"})

    def test_health(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"status": "ok"})

    def test_detailed_health_leaks_no_secrets(self):
        body = self.client.get("/api/health").get_json()
        self.assertEqual(body["status"], "ok")
        serialized = str(body)
        self.assertNotIn(config.AUTH_SECRET, serialized)
        self.assertNotIn(TEST_BOT_TOKEN, serialized)

    def test_room_endpoint(self):
        body = self.client.get("/api/rooms/gossip-main").get_json()
        self.assertEqual(body["id"], "gossip-main")
        self.assertEqual(body["maxParticipants"], 20)
        self.assertTrue(body["live"])
        self.assertFalse(body["locked"])

    def test_unknown_room(self):
        self.assertEqual(self.client.get("/api/rooms/does-not-exist").status_code, 404)

    def test_json_error_shape(self):
        body = self.client.get("/api/nope").get_json()
        self.assertIn("code", body)
        self.assertIn("message", body)


class TelegramVerificationTests(unittest.TestCase):
    def test_valid_signature(self):
        data = make_init_data(555001, "Ada", "Lovelace", username="ada")
        verified = verify_init_data(data, TEST_BOT_TOKEN)
        self.assertEqual(str(verified["user"]["id"]), "555001")

    def test_tampered_signature_rejected(self):
        data = make_init_data(555002, tamper=True)
        with self.assertRaises(TelegramAuthError) as ctx:
            verify_init_data(data, TEST_BOT_TOKEN)
        self.assertEqual(ctx.exception.code, "TELEGRAM_SIGNATURE_INVALID")

    def test_modified_user_field_rejected(self):
        data = make_init_data(555003, "Real")
        forged = data.replace("Real", "Fake")
        with self.assertRaises(TelegramAuthError):
            verify_init_data(forged, TEST_BOT_TOKEN)

    def test_wrong_bot_token_rejected(self):
        data = make_init_data(555004, bot_token="999:OTHER-TOKEN")
        with self.assertRaises(TelegramAuthError):
            verify_init_data(data, TEST_BOT_TOKEN)

    def test_expired_init_data_rejected(self):
        old = int(time.time()) - (config.TELEGRAM_INIT_DATA_TTL + 600)
        data = make_init_data(555005, auth_date=old)
        with self.assertRaises(TelegramAuthError) as ctx:
            verify_init_data(data, TEST_BOT_TOKEN)
        self.assertEqual(ctx.exception.code, "TELEGRAM_INIT_DATA_EXPIRED")

    def test_profile_never_invents_username(self):
        from telegram.identity import build_profile

        profile = build_profile(verify_init_data(make_init_data(555006, "Solo", ""), TEST_BOT_TOKEN))
        self.assertIsNone(profile.username)
        self.assertEqual(profile.display_name, "Solo")


class SessionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(start_background=False)
        cls.client = cls.app.test_client()
        cls.fb = FakeFirebase()
        cls.fb.install(auth_routes)

    def session_for(self, uid, telegram_id=None, **kw):
        body = {"firebaseIdToken": self.fb.token_for(uid), "roomId": "gossip-main"}
        if telegram_id:
            body["initData"] = make_init_data(telegram_id, **kw)
        return self.client.post("/api/auth/session", json=body)

    def test_invalid_firebase_token_rejected(self):
        res = self.client.post(
            "/api/auth/session", json={"firebaseIdToken": "garbage", "roomId": "gossip-main"}
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["code"], "FIREBASE_TOKEN_INVALID")

    def test_missing_firebase_token_rejected(self):
        res = self.client.post("/api/auth/session", json={"roomId": "gossip-main"})
        self.assertEqual(res.status_code, 401)

    def test_telegram_identity_used_for_profile(self):
        res = self.session_for(
            "uid-a", 900001, first_name="Grace", last_name="Hopper", username="grace"
        )
        self.assertEqual(res.status_code, 200)
        user = res.get_json()["user"]
        self.assertEqual(user["id"], "tg_900001")
        self.assertEqual(user["displayName"], "Grace Hopper")
        self.assertEqual(user["username"], "grace")
        self.assertEqual(user["role"], "participant")

    def test_client_cannot_claim_role_or_profile(self):
        res = self.client.post(
            "/api/auth/session",
            json={
                "firebaseIdToken": self.fb.token_for("uid-b"),
                "roomId": "gossip-main",
                "initData": make_init_data(900002, first_name="Real", username="realuser"),
                # all of the following must be ignored
                "role": "owner",
                "displayName": "I AM OWNER",
                "username": "fake_owner",
                "telegramId": "1000001",
                "photoUrl": "https://evil.example/x.png",
            },
        )
        user = res.get_json()["user"]
        self.assertEqual(user["role"], "participant")
        self.assertEqual(user["displayName"], "Real")
        self.assertEqual(user["username"], "realuser")
        self.assertEqual(user["telegramId"], "900002")

    def test_configured_owner_and_moderator(self):
        owner = self.session_for("uid-owner", 1000001).get_json()["user"]
        self.assertEqual(owner["role"], "owner")
        moderator = self.session_for("uid-mod", 1000002).get_json()["user"]
        self.assertEqual(moderator["role"], "moderator")

    def test_tampered_init_data_rejected(self):
        res = self.client.post(
            "/api/auth/session",
            json={
                "firebaseIdToken": self.fb.token_for("uid-c"),
                "roomId": "gossip-main",
                "initData": make_init_data(900003, tamper=True),
            },
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["code"], "TELEGRAM_SIGNATURE_INVALID")

    def test_stable_identity_across_firebase_users(self):
        first = self.session_for("uid-d1", 900004).get_json()["user"]["id"]
        second = self.session_for("uid-d2", 900004).get_json()["user"]["id"]
        self.assertEqual(first, second, "one Telegram id must map to one VC identity")

    def test_ticket_issue_and_single_use(self):
        token = self.session_for("uid-e", 900005).get_json()["token"]
        res = self.client.post(
            "/api/auth/ticket",
            json={"roomId": "gossip-main"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(res.status_code, 200)
        ticket = res.get_json()["ticket"]
        self.assertLessEqual(res.get_json()["expiresIn"], 90)

        redeem_ticket(ticket, "gossip-main")
        with self.assertRaises(TicketError) as ctx:
            redeem_ticket(ticket, "gossip-main")
        self.assertEqual(ctx.exception.code, "TICKET_ALREADY_USED")

    def test_ticket_requires_session(self):
        self.assertEqual(self.client.post("/api/auth/ticket", json={}).status_code, 401)
        res = self.client.post(
            "/api/auth/ticket", json={}, headers={"Authorization": "Bearer nonsense.sig"}
        )
        self.assertEqual(res.status_code, 401)

    def test_ticket_room_binding(self):
        token = self.session_for("uid-f", 900006).get_json()["token"]
        ticket = self.client.post(
            "/api/auth/ticket",
            json={"roomId": "gossip-main"},
            headers={"Authorization": f"Bearer {token}"},
        ).get_json()["ticket"]
        with self.assertRaises(TicketError):
            redeem_ticket(ticket, "another-room")

    def test_expired_ticket_rejected(self):
        session = issue_session(
            {
                "id": "tg_900007",
                "firebase_uid": "uid-g",
                "telegram_id": "900007",
                "display_name": "Old",
                "username": None,
                "photo_url": None,
                "source": "telegram",
            },
            "gossip-main",
        )
        ticket = issue_ticket(session["payload"], "gossip-main")
        ticket["payload"]["exp"] = int(time.time()) - 5
        from auth.tickets import _encode  # noqa: PLC0415

        expired = _encode(ticket["payload"])
        with self.assertRaises(TicketError) as ctx:
            redeem_ticket(expired, "gossip-main")
        self.assertEqual(ctx.exception.code, "TOKEN_EXPIRED")

    def test_me_endpoint(self):
        token = self.session_for("uid-h", 900008, first_name="Mee").get_json()["token"]
        body = self.client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).get_json()
        self.assertEqual(body["telegramId"], "900008")
        self.assertEqual(body["role"], "participant")


class OriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = create_app(start_background=False).test_client()

    def test_allowed_origin_gets_cors(self):
        res = self.client.get("/health", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "http://localhost:5173")

    def test_telegram_origin_allowed(self):
        res = self.client.get("/health", headers={"Origin": "https://web.telegram.org"})
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "https://web.telegram.org")

    def test_unknown_origin_gets_no_cors(self):
        res = self.client.get("/health", headers={"Origin": "https://evil.example"})
        self.assertIsNone(res.headers.get("Access-Control-Allow-Origin"))

    def test_oversized_body_rejected(self):
        res = self.client.post(
            "/api/auth/session",
            data="x" * (config.MAX_HTTP_BODY_BYTES + 1024),
            content_type="application/json",
        )
        self.assertIn(res.status_code, (400, 413))


if __name__ == "__main__":
    unittest.main(verbosity=2)
