"""End-to-end websocket tests against a real Flask server + real sockets."""

from __future__ import annotations

import json
import threading
import time
import unittest

from conftest import FakeFirebase, make_init_data  # noqa: E402

import routes.auth as auth_routes  # noqa: E402
import simple_websocket  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from config import config  # noqa: E402
from rooms.manager import room_manager  # noqa: E402
from websocket.heartbeat import sweep_once  # noqa: E402

PORT = 18711
HTTP = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}"
ROOM = "gossip-main"


class Client:
    def __init__(self, suite, uid: str, telegram_id: int, **profile):
        self.suite = suite
        self.uid = uid
        self.telegram_id = telegram_id
        self.profile = profile
        self.token = None
        self.user_id = None
        self.role = None
        self.ws = None

    def authenticate(self):
        body = {
            "firebaseIdToken": self.suite.fb.token_for(self.uid),
            "roomId": ROOM,
            "initData": make_init_data(self.telegram_id, **self.profile),
        }
        res = self.suite.http.post("/api/auth/session", json=body)
        assert res.status_code == 200, res.get_data(as_text=True)
        data = res.get_json()
        self.token = data["token"]
        self.user_id = data["user"]["id"]
        self.role = data["user"]["role"]
        return data

    def ticket(self):
        res = self.suite.http.post(
            "/api/auth/ticket",
            json={"roomId": ROOM},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        return res.get_json()["ticket"]

    def connect(self, ticket=None):
        self.ws = simple_websocket.Client(f"{WS}/ws/voice/{ROOM}?token={ticket or self.ticket()}")
        return self

    def send(self, event, payload=None):
        self.ws.send(json.dumps({"type": event, "payload": payload or {}}))

    def send_raw(self, raw):
        self.ws.send(raw)

    def join(self):
        self.send("JOIN_ROOM", {"roomId": ROOM})
        return self.expect("ROOM_STATE")

    def expect(self, event, timeout=2.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.receive(timeout=max(0.05, deadline - time.time()))
            except Exception:
                return None
            if raw is None:
                continue
            frame = json.loads(raw)
            if frame.get("type") == event:
                return frame
        return None

    def drain(self, timeout=0.6):
        frames = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.receive(timeout=max(0.05, deadline - time.time()))
            except Exception:
                break
            if raw is None:
                break
            try:
                frames.append(json.loads(raw))
            except ValueError:
                pass
        return frames

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


SDP_AUDIO = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


class VoiceTests(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.app = create_app(start_background=False)
        cls.http = cls.app.test_client()
        cls.fb = FakeFirebase()
        cls.fb.install(auth_routes)
        cls.server = make_server("127.0.0.1", PORT, cls.app, threaded=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        room_manager.reset(ROOM)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        room_manager.reset(ROOM)

    def make(self, uid, telegram_id, **profile):
        client = Client(self, uid, telegram_id, **profile)
        client.authenticate()
        client.connect()
        self.clients.append(client)
        return client

    # ------------------------------------------------------------ identity
    def test_invalid_ticket_rejected(self):
        ws = simple_websocket.Client(f"{WS}/ws/voice/{ROOM}?token=not-a-real-ticket")
        frame = json.loads(ws.receive(timeout=2))
        self.assertEqual(frame["type"], "ERROR")
        self.assertEqual(frame["payload"]["code"], "UNAUTHORIZED")
        ws.close()

    def test_ticket_cannot_be_replayed(self):
        client = Client(self, "replay", 800101)
        client.authenticate()
        ticket = client.ticket()
        client.connect(ticket)
        self.clients.append(client)
        second = simple_websocket.Client(f"{WS}/ws/voice/{ROOM}?token={ticket}")
        frame = json.loads(second.receive(timeout=2))
        self.assertEqual(frame["payload"]["code"], "UNAUTHORIZED")
        second.close()

    def test_unknown_room_rejected(self):
        client = Client(self, "badroom", 800102)
        client.authenticate()
        ws = simple_websocket.Client(f"{WS}/ws/voice/no-such-room?token={client.ticket()}")
        frame = json.loads(ws.receive(timeout=2))
        self.assertEqual(frame["payload"]["code"], "ROOM_NOT_FOUND")
        ws.close()

    # ----------------------------------------------------------- room flow
    def test_join_state_and_roles(self):
        owner = self.make("o1", 1000001, first_name="Olivia", username="olivia")
        state = owner.join()
        self.assertEqual(state["payload"]["room"]["id"], ROOM)
        self.assertEqual(state["payload"]["currentUserId"], owner.user_id)
        joined = owner.expect("ROOM_JOINED")
        self.assertEqual(joined["payload"]["role"], "owner")
        self.assertTrue(joined["payload"]["permissions"]["canEnd"])
        self.assertIsNotNone(owner.expect("MESSAGE_HISTORY"))

        member = self.make("m1", 800201, first_name="Mia")
        member.join()
        self.assertEqual(member.expect("ROOM_JOINED")["payload"]["role"], "participant")
        evt = owner.expect("PARTICIPANT_JOINED")
        self.assertEqual(evt["payload"]["participant"]["id"], member.user_id)
        self.assertEqual(evt["payload"]["participant"]["displayName"], "Mia User")

    def test_leave_broadcasts(self):
        a = self.make("a1", 1000001)
        a.join()
        b = self.make("b1", 800202)
        b.join()
        a.drain(0.4)
        b.send("LEAVE_ROOM")
        left = a.expect("PARTICIPANT_LEFT")
        self.assertEqual(left["payload"]["userId"], b.user_id)

    def test_room_full(self):
        room = room_manager.get(ROOM)
        room.max_participants = 2
        try:
            a = self.make("f1", 1000001)
            a.join()
            b = self.make("f2", 800301)
            b.join()
            c = self.make("f3", 800302)
            c.send("JOIN_ROOM", {"roomId": ROOM})
            err = c.expect("ERROR")
            self.assertEqual(err["payload"]["code"], "ROOM_FULL")
        finally:
            room.max_participants = config.MAX_PARTICIPANTS

    def test_duplicate_session(self):
        first = self.make("d1", 800401)
        first.join()
        second = Client(self, "d1", 800401)
        second.authenticate()
        second.connect()
        self.clients.append(second)
        second.join()
        err = first.expect("ERROR")
        self.assertEqual(err["payload"]["code"], "DUPLICATE_SESSION")
        state = second.expect("ROOM_STATE", timeout=0.1) or {"payload": {}}
        self.assertEqual(len(room_manager.get(ROOM).participants()), 1)

    def test_reconnect_leaves_no_ghost(self):
        a = self.make("r1", 1000001)
        a.join()
        b = self.make("r2", 800501)
        b.join()
        b.close()
        self.assertIsNotNone(a.expect("PARTICIPANT_LEFT"))
        b2 = Client(self, "r2", 800501)
        b2.authenticate()
        b2.connect()
        self.clients.append(b2)
        state = b2.join()
        ids = [p["id"] for p in state["payload"]["participants"]]
        self.assertEqual(ids.count(b2.user_id), 1)

    # ------------------------------------------------------ mute / speaking
    def test_mute_and_speaking(self):
        a = self.make("s1", 1000001)
        a.join()
        b = self.make("s2", 800601)
        b.join()
        a.drain(0.4)

        b.send("MUTE_CHANGED", {"userId": b.user_id, "muted": False})
        frame = a.expect("MUTE_CHANGED")
        self.assertFalse(frame["payload"]["muted"])

        b.send("SPEAKING_CHANGED", {"userId": b.user_id, "speaking": True})
        frame = a.expect("SPEAKING_CHANGED")
        self.assertTrue(frame["payload"]["speaking"])

        b.send("MUTE_CHANGED", {"userId": a.user_id, "muted": True})
        self.assertEqual(b.expect("ERROR")["payload"]["code"], "FORBIDDEN")

    # -------------------------------------------------------- moderation
    def test_moderation_permissions(self):
        owner = self.make("mo1", 1000001)
        owner.join()
        mod = self.make("mo2", 1000002)
        mod.join()
        member = self.make("mo3", 800701)
        member.join()
        owner.drain(0.4)
        mod.drain(0.4)

        member.send("MODERATE", {"action": "remove", "targetUserId": owner.user_id})
        self.assertEqual(member.expect("ERROR")["payload"]["code"], "FORBIDDEN")

        mod.send("MODERATE", {"action": "mute", "targetUserId": member.user_id})
        updated = member.expect("PARTICIPANT_UPDATED")
        self.assertTrue(updated["payload"]["participant"]["muted"])

        mod.send("MODERATE", {"action": "restrict", "targetUserId": member.user_id})
        self.assertTrue(member.expect("PARTICIPANT_UPDATED")["payload"]["participant"]["restricted"])
        member.send("MUTE_CHANGED", {"userId": member.user_id, "muted": False})
        self.assertEqual(member.expect("ERROR")["payload"]["code"], "RESTRICTED")

        mod.send("MODERATE", {"action": "allow", "targetUserId": member.user_id})
        self.assertFalse(member.expect("PARTICIPANT_UPDATED")["payload"]["participant"]["restricted"])

        mod.send("MODERATE", {"action": "mute", "targetUserId": owner.user_id})
        self.assertEqual(mod.expect("ERROR")["payload"]["code"], "FORBIDDEN")

    def test_kick_and_cooldown(self):
        owner = self.make("k1", 1000001)
        owner.join()
        member = self.make("k2", 800801)
        member.join()
        owner.drain(0.4)

        owner.send("MODERATE", {"action": "remove", "targetUserId": member.user_id})
        kicked = member.expect("KICKED")
        self.assertEqual(kicked["payload"]["userId"], member.user_id)
        self.assertGreater(kicked["payload"]["cooldownSeconds"], 0)
        self.assertEqual(owner.expect("PARTICIPANT_LEFT")["payload"]["userId"], member.user_id)

        again = Client(self, "k2", 800801)
        again.authenticate()
        again.connect()
        self.clients.append(again)
        again.send("JOIN_ROOM", {"roomId": ROOM})
        self.assertEqual(again.expect("ERROR")["payload"]["code"], "KICK_COOLDOWN")

    def test_lock_unlock_keeps_existing_users(self):
        owner = self.make("l1", 1000001)
        owner.join()
        member = self.make("l2", 800901)
        member.join()
        owner.drain(0.4)

        owner.send("MODERATE", {"action": "lock"})
        self.assertIsNotNone(member.expect("ROOM_LOCKED"))
        member.send("CHAT_MESSAGE", {"text": "still connected"})
        self.assertIsNotNone(member.expect("MESSAGE"))

        stranger = self.make("l3", 800902)
        stranger.send("JOIN_ROOM", {"roomId": ROOM})
        self.assertEqual(stranger.expect("ERROR")["payload"]["code"], "ROOM_LOCKED")

        owner.send("MODERATE", {"action": "unlock"})
        self.assertIsNotNone(member.expect("ROOM_UNLOCKED"))
        stranger.send("JOIN_ROOM", {"roomId": ROOM})
        self.assertIsNotNone(stranger.expect("ROOM_STATE"))

    def test_only_owner_ends_room(self):
        owner = self.make("e1", 1000001)
        owner.join()
        member = self.make("e2", 801001)
        member.join()
        owner.drain(0.4)

        member.send("END_ROOM")
        self.assertEqual(member.expect("ERROR")["payload"]["code"], "FORBIDDEN")

        owner.send("END_ROOM")
        self.assertIsNotNone(owner.expect("ROOM_ENDED"))
        self.assertIsNotNone(member.expect("ROOM_ENDED"))

    # --------------------------------------------------------------- chat
    def test_chat_and_history(self):
        a = self.make("c1", 1000001)
        a.join()
        b = self.make("c2", 801101)
        b.join()
        a.drain(0.4)

        b.send("CHAT_MESSAGE", {"text": "ddos"})   # plain text, never a command
        msg = a.expect("MESSAGE")
        self.assertEqual(msg["payload"]["message"]["text"], "ddos")

        b.send("CHAT_MESSAGE", {"text": "   "})
        self.assertEqual(b.expect("ERROR")["payload"]["code"], "EMPTY_MESSAGE")

        b.send("CHAT_MESSAGE", {"text": "x" * 501})
        self.assertEqual(b.expect("ERROR")["payload"]["code"], "MESSAGE_TOO_LONG")

        c = self.make("c3", 801102)
        state = c.join()
        history = c.expect("MESSAGE_HISTORY")
        self.assertTrue(any(m["text"] == "ddos" for m in history["payload"]["messages"]))
        self.assertIsNotNone(state)

    # ------------------------------------------------------------- WebRTC
    def test_webrtc_signalling(self):
        a = self.make("w1", 1000001)
        a.join()
        b = self.make("w2", 801201)
        b.join()
        a.drain(0.4)

        a.send("OFFER", {"fromUserId": a.user_id, "toUserId": b.user_id,
                         "sdp": {"type": "offer", "sdp": SDP_AUDIO}})
        offer = b.expect("OFFER")
        self.assertEqual(offer["payload"]["fromUserId"], a.user_id)
        self.assertEqual(offer["payload"]["sdp"]["sdp"], SDP_AUDIO)  # relayed unmodified

        b.send("ANSWER", {"fromUserId": b.user_id, "toUserId": a.user_id,
                          "sdp": {"type": "answer", "sdp": SDP_AUDIO}})
        self.assertIsNotNone(a.expect("ANSWER"))

        b.send("ICE_CANDIDATE", {"fromUserId": b.user_id, "toUserId": a.user_id,
                                 "candidate": {"candidate": "candidate:1 1 udp 1 1.2.3.4 1 typ host",
                                               "sdpMid": "0", "sdpMLineIndex": 0}})
        self.assertIsNotNone(a.expect("ICE_CANDIDATE"))

    def test_signalling_guards(self):
        a = self.make("g1", 1000001)
        a.join()
        b = self.make("g2", 801301)
        b.join()
        a.drain(0.4)

        a.send("OFFER", {"fromUserId": b.user_id, "toUserId": b.user_id,
                         "sdp": {"type": "offer", "sdp": SDP_AUDIO}})
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "FORBIDDEN")

        a.send("OFFER", {"fromUserId": a.user_id, "toUserId": "tg_999999999",
                         "sdp": {"type": "offer", "sdp": SDP_AUDIO}})
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "TARGET_NOT_FOUND")

        a.send("OFFER", {"fromUserId": a.user_id, "toUserId": b.user_id,
                         "sdp": {"type": "offer", "sdp": SDP_AUDIO + "m=video 9 RTP/SAVPF 96\r\n"}})
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "VIDEO_NOT_ALLOWED")

        a.send("OFFER", {"fromUserId": a.user_id, "toUserId": b.user_id,
                         "sdp": {"type": "offer", "sdp": "x" * (config.MAX_SDP_BYTES + 10)}})
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "PAYLOAD_TOO_LARGE")

    # ------------------------------------------------------ robustness
    def test_invalid_input_never_crashes(self):
        a = self.make("i1", 1000001)
        a.join()
        a.send_raw("this is not json")
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "INVALID_JSON")
        a.send_raw(json.dumps([1, 2, 3]))
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "INVALID_MESSAGE")
        a.send_raw(json.dumps({"type": "DROP_TABLE"}))
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "UNKNOWN_EVENT")
        a.send_raw(json.dumps({"type": "CHAT_MESSAGE", "payload": "nope"}))
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "INVALID_PAYLOAD")
        a.send("MUTE_CHANGED", {"userId": a.user_id, "muted": "yes"})
        self.assertEqual(a.expect("ERROR")["payload"]["code"], "INVALID_PAYLOAD")
        a.send("CHAT_MESSAGE", {"text": "still alive"})
        self.assertIsNotNone(a.expect("MESSAGE"))

    def test_rate_limiting(self):
        a = self.make("rl1", 1000001)
        a.join()
        a.drain(0.3)
        for _ in range(80):
            a.send("CHAT_MESSAGE", {"text": "spam"})
        codes = {f["payload"].get("code") for f in a.drain(2.0) if f["type"] == "ERROR"}
        self.assertIn("RATE_LIMITED", codes)

    def test_stale_connection_cleanup(self):
        a = self.make("st1", 1000001)
        a.join()
        b = self.make("st2", 801401)
        b.join()
        a.drain(0.4)

        from websocket.voice import registry_snapshot

        for conn in registry_snapshot():
            if conn.user_id == b.user_id:
                conn.last_seen = time.time() - (config.WS_STALE_AFTER_SECONDS + 60)
        reaped = sweep_once()
        self.assertGreaterEqual(reaped, 1)
        self.assertEqual(a.expect("PARTICIPANT_LEFT")["payload"]["userId"], b.user_id)
        self.assertIsNone(room_manager.get(ROOM).get(b.user_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
