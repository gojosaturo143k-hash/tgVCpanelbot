# Aerogram Voice Chat — Flask backend

Clean-rewrite backend for the React/Vite Telegram-style group voice chat.

```
Telegram group ──/vc──► Bot ──"Join Voice Chat"──► Mini App (React/Vite)
                                                        │
                         signed initData + Firebase anonymous ID token
                                                        ▼
                                        Flask backend (this service)
                                        verify → session → 75s ticket
                                                        │
                                   WebSocket /ws/voice/<room_id>?token=…
                                                        │  signalling only
                                                        ▼
                                          WebRTC P2P mesh ─► voice audio
```

**Audio never touches Flask.** Nothing is recorded, stored, uploaded or proxied.
No video. Max **20 participants** per room (mesh, no SFU).

Stack: Python 3 · Flask · Flask-Sock · simple-websocket · Firebase Admin SDK.
No Node, FastAPI, Django, Socket.IO, Gunicorn, Uvicorn, Postgres or Redis.

---

## 1. Run it

```bash
pip install -r backend/requirements.txt
python backend/app.py            # -> 0.0.0.0:${PORT:-10000}
```

```bash
curl http://localhost:10000/           # {"status":"ok","service":"voice-chat-backend"}
curl http://localhost:10000/health     # {"status":"ok"}
curl http://localhost:10000/api/rooms/gossip-main
```

Local development without Firebase/Telegram keys:

```bash
DEV_MODE=true AUTH_SECRET=dev-secret python backend/app.py
```

In `DEV_MODE` the backend accepts a `dev:<uid>` placeholder instead of a real
Firebase ID token, and the first joiner may adopt an ownerless room. Both
behaviours are **disabled** when `DEV_MODE=false`.

## 2. Tests

```bash
python backend/tests/run_tests.py
# or
cd backend && python -m unittest discover -s tests -t tests -v
```

The runner checks its dependencies first and refuses to run (exit code 2)
rather than reporting a false pass. Firebase is mocked (`FakeFirebase`);
Telegram init data is **genuinely signed** with a test bot token, so signature
verification is exercised for real.

Coverage: health/root endpoints · Firebase token verification + rejection ·
Telegram signature, tampering, expiry, wrong-token · identity stability ·
ticket issue/expiry/replay/room-binding · origin validation · oversized body ·
join/leave/room-full/duplicate session/reconnect · mute · speaking ·
moderation matrix · lock/unlock · kick + cooldown · end room · chat +
validation · OFFER/ANSWER/ICE relay and guards · rate limits · stale cleanup.

---

## 3. Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PORT` | Render sets it | `10000` | Listen port |
| `DEV_MODE` | yes | `false` | `true` only for local development |
| `AUTH_SECRET` | **prod** | ephemeral | HMAC key for sessions/tickets |
| `FRONTEND_ORIGIN` | **prod** | – | Comma-separated allowed origins |
| `FIREBASE_PROJECT_ID` | **prod** | – | Service account project |
| `FIREBASE_CLIENT_EMAIL` | **prod** | – | Service account email |
| `FIREBASE_PRIVATE_KEY` | **prod** | – | Service account key (`\n` escapes kept) |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | alt | – | Whole service-account JSON instead of the three above |
| `TELEGRAM_BOT_TOKEN` | **prod** | – | Verifies Mini App init data |
| `TELEGRAM_REQUIRED` | no | `false` | Reject non-Telegram logins |
| `TELEGRAM_INIT_DATA_TTL` | no | `86400` | Max init-data age (seconds) |
| `OWNER_TELEGRAM_IDS` | no | – | CSV of Telegram ids granted **owner** |
| `MODERATOR_TELEGRAM_IDS` | no | – | CSV of Telegram ids granted **moderator** |
| `MAX_PARTICIPANTS` | no | `20` | Hard room cap |
| `DEFAULT_ROOM_ID` / `_TITLE` / `_EMOJI` | no | `gossip-main` / `Gossip` / 💬 | Default room |
| `ALLOW_ROOM_AUTOCREATE` | no | `false` | Create unknown rooms on demand |
| `CHAT_ENABLED` / `MAX_CHAT_LENGTH` / `MAX_CHAT_HISTORY` | no | `true` / `500` / `50` | Chat |
| `WS_PING_INTERVAL` / `WS_STALE_AFTER_SECONDS` | no | `25` / `90` | Heartbeat |
| `MAX_CONNECTIONS_PER_IP` / `_USER` | no | `8` / `3` | Connection caps |
| `SESSION_TTL_SECONDS` / `TICKET_TTL_SECONDS` | no | `3600` / `75` | Token lifetimes |
| `KICK_COOLDOWN_SECONDS` | no | `60` | Rejoin cooldown after a kick |

Secrets are never logged, never returned by any endpoint and never sent to the
frontend. `/api/health` only reports booleans such as `firebaseConfigured`.

---

## 4. Render deployment

* **Build command:** `pip install -r backend/requirements.txt`
* **Start command:** `python backend/app.py`
* **Health check path:** `/health`
* **Instances:** 1 (room state is in-memory)

`render.yaml` at the repository root declares exactly this. Do **not** set
`PORT` manually — Render injects it and `app.py` reads
`int(os.getenv("PORT", "10000"))` and binds `0.0.0.0`.

Websocket URL after deploy: `wss://<service>.onrender.com/ws/voice/gossip-main`.

---

## 5. Firebase setup

1. Firebase console → create/choose a project.
2. **Authentication → Sign-in method → Anonymous → enable.**
3. **Project settings → Service accounts → Generate new private key.**
4. Put `project_id`, `client_email`, `private_key` into the Render env vars
   (`FIREBASE_PROJECT_ID`, `FIREBASE_CLIENT_EMAIL`, `FIREBASE_PRIVATE_KEY`) —
   or paste the whole file into `FIREBASE_SERVICE_ACCOUNT_JSON`.
5. Frontend uses only the **web** config (`VITE_FIREBASE_*`), which is public.
   The service account never leaves the backend.

## 6. Telegram Bot / Mini App setup

1. `@BotFather` → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. `@BotFather` → `/newapp` (or *Bot Settings → Menu Button*) → point the Web
   App URL at your deployed frontend.
3. In your group the bot answers `/vc` with an inline keyboard button of type
   `web_app` pointing at the same URL. Telegram then supplies signed
   `initData` to the page.
4. Add your own numeric Telegram id to `OWNER_TELEGRAM_IDS`
   (any moderators to `MODERATOR_TELEGRAM_IDS`).
5. Set `TELEGRAM_REQUIRED=true` so the VC can only be entered from Telegram.

The backend never asks the Bot API for private account data; it uses only the
fields inside the signed Mini App context (id, first/last name, username when
it exists, photo_url when provided, language_code).

---

## 7. HTTP API

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/` | – | `{"status":"ok","service":"voice-chat-backend"}` |
| `GET` | `/health` | – | `{"status":"ok"}` |
| `GET` | `/api/health` | – | uptime, counts, config booleans (no secrets) |
| `GET` | `/api/rooms` | – | room list |
| `GET` | `/api/rooms/<room_id>` | – | room info + participants |
| `POST` | `/api/auth/session` | body | `{firebaseIdToken, initData?, roomId}` → session token |
| `POST` | `/api/auth/ticket` | Bearer session | single-use ~75 s websocket ticket |
| `GET` | `/api/auth/me` | Bearer session | linked identity + computed role |
| `GET` | `/api/auth/config` | – | public flags for the frontend |

Every HTTP error is `{"code": "...", "message": "..."}`. Stack traces are
logged server-side only.

## 8. WebSocket protocol

`/ws/voice/<room_id>?token=<ticket>` — frames are always
`{"type": "...", "payload": {...}}`.

**Client → server:** `JOIN_ROOM`, `LEAVE_ROOM`, `MUTE_CHANGED`,
`SPEAKING_CHANGED`, `MODERATE`, `END_ROOM`, `CHAT_MESSAGE`, `OFFER`, `ANSWER`,
`ICE_CANDIDATE`.

**Server → client:** `ROOM_STATE`, `ROOM_JOINED`, `PARTICIPANT_JOINED`,
`PARTICIPANT_LEFT`, `PARTICIPANT_UPDATED`, `MUTE_CHANGED`, `SPEAKING_CHANGED`,
`ROOM_LOCKED`, `ROOM_UNLOCKED`, `ROOM_ENDED`, `KICKED`, `MESSAGE`,
`MESSAGE_HISTORY`, `OFFER`, `ANSWER`, `ICE_CANDIDATE`, `ERROR`.

`ROOM_STATE` payload:

```json
{
  "room": {"id":"gossip-main","title":"Gossip","emoji":"💬","live":true,
           "locked":false,"ownerId":"tg_1000001","participantCount":2,
           "memberCount":2,"maxParticipants":20,"chatEnabled":true},
  "participants": [
    {"id":"tg_1000001","userId":"tg_1000001","name":"Grace Hopper",
     "displayName":"Grace Hopper","username":"grace","photoUrl":"https://…",
     "avatarUrl":"https://…","telegramId":"1000001","role":"owner",
     "muted":false,"speaking":true,"restricted":false,"joined":true,
     "joinedAt":1700000000000}
  ],
  "currentUserId": "tg_1000001",
  "role": "owner",
  "permissions": {"canMuteOthers":true,"canAllowSpeak":true,"canRestrict":true,
                  "canRemove":true,"canModerate":true,"canLock":true,
                  "canEnd":true,"canSpeak":true,"canChat":true}
}
```

Error codes: `INVALID_JSON`, `INVALID_MESSAGE`, `INVALID_TYPE`,
`INVALID_PAYLOAD`, `PAYLOAD_TOO_LARGE`, `UNKNOWN_EVENT`, `UNAUTHORIZED`,
`ORIGIN_NOT_ALLOWED`, `FORBIDDEN`, `NOT_IN_ROOM`, `ROOM_NOT_FOUND`,
`ROOM_MISMATCH`, `ROOM_LOCKED`, `ROOM_FULL`, `KICK_COOLDOWN`,
`DUPLICATE_SESSION`, `TARGET_NOT_FOUND`, `INVALID_TARGET`, `INVALID_ACTION`,
`RESTRICTED`, `CHAT_DISABLED`, `EMPTY_MESSAGE`, `MESSAGE_TOO_LONG`,
`VIDEO_NOT_ALLOWED`, `RATE_LIMITED`, `SERVER_ERROR`.

## 9. WebRTC signalling

1. `ROOM_STATE` gives the roster; `PARTICIPANT_JOINED` announces newcomers.
2. The peer with the **smaller user id** creates the offer (deterministic, so
   exactly one offer per pair, no glare storms).
3. `OFFER` → validated (authenticated sender, same room, target present, size
   caps, `m=video` rejected) and relayed **byte-for-byte unmodified**.
4. `ANSWER` back, then `ICE_CANDIDATE` trickling both ways.
5. Media flows directly between browsers. SDP is relayed and forgotten — never
   persisted.

## 10. Roles & permissions (server-side only)

| | owner | moderator | participant |
|---|---|---|---|
| join / leave / self-mute / speaking / chat | ✅ | ✅ | ✅ |
| mute · allow · restrict · remove others | ✅ | ✅ | ❌ |
| lock / unlock room | ✅ | ❌ | ❌ |
| end room | ✅ | ❌ | ❌ |

Owners/moderators come from `OWNER_TELEGRAM_IDS` / `MODERATOR_TELEGRAM_IDS`
(verified Telegram ids) or from the room's recorded owner. A browser sending
`role: "owner"` is ignored everywhere. Owners cannot be moderated; moderators
cannot moderate each other. Locking never disconnects people already inside.

## 11. Abuse protection

Per-connection token buckets per event type plus a global bucket; per-IP HTTP
buckets; max 8 sockets per IP and 3 per user; 64 KB frame / 48 KB SDP / 8 KB
ICE / 64 KB HTTP body caps; strict schema validation; origin checks; single-use
tickets; kick cooldown; duplicate-session takeover; 25 s pings plus a 10 s
stale-socket reaper.

These are **application-layer protections only** — they do not make the service
DDoS-proof; network-level attacks must be handled by the provider/CDN.

## 12. File tree

```
backend/
├── app.py                  # single Flask app + entry point
├── config.py               # env-driven configuration
├── requirements.txt
├── .env.example
├── firebase/  __init__.py  admin.py            # Admin SDK + ID-token verification
├── telegram/  __init__.py  init_data.py  identity.py
├── auth/      __init__.py  identity.py  tickets.py
├── rooms/     __init__.py  manager.py  room.py  participant.py
├── websocket/ __init__.py  voice.py  protocol.py  heartbeat.py
├── security/  __init__.py  rate_limit.py  validation.py  origin.py
├── routes/    __init__.py  health.py  auth.py  rooms.py
└── tests/     conftest.py  test_auth.py  test_voice.py  run_tests.py
```

One Flask app, one websocket implementation, one room manager, one auth system.
