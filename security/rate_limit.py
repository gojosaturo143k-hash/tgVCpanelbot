"""Application-layer abuse protection.

Token buckets per connection/event, per-IP HTTP buckets and hard caps on
simultaneous connections. These are ordinary application protections - they are
explicitly **not** DDoS protection.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, Tuple

from config import config


class TokenBucket:
    __slots__ = ("capacity", "refill", "tokens", "updated")

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = float(capacity)
        self.refill = float(refill_per_second)
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class EventRateLimiter:
    """One instance per websocket connection."""

    def __init__(self) -> None:
        cap, refill = config.EVENT_RATE_LIMITS["__global__"]
        self._global = TokenBucket(cap, refill)
        self._buckets: Dict[str, TokenBucket] = {}
        self.violations = 0

    def allow(self, event: str) -> bool:
        if not self._global.allow():
            self.violations += 1
            return False
        bucket = self._buckets.get(event)
        if bucket is None:
            cap, refill = config.EVENT_RATE_LIMITS.get(event, (20, 4.0))
            bucket = TokenBucket(cap, refill)
            self._buckets[event] = bucket
        if not bucket.allow():
            self.violations += 1
            return False
        return True

    def note_violation(self) -> int:
        self.violations += 1
        return self.violations

    @property
    def exhausted(self) -> bool:
        return self.violations > config.MAX_PROTOCOL_VIOLATIONS


class HttpRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: Dict[Tuple[str, str], TokenBucket] = {}
        self._swept = time.monotonic()

    def allow(self, scope: str, key: str) -> bool:
        capacity, refill = config.HTTP_RATE_LIMITS.get(scope, (60, 2.0))
        with self._lock:
            self._sweep()
            bucket = self._buckets.get((scope, key))
            if bucket is None:
                bucket = TokenBucket(capacity, refill)
                self._buckets[(scope, key)] = bucket
            return bucket.allow()

    def _sweep(self) -> None:
        now = time.monotonic()
        if now - self._swept < 300:
            return
        self._swept = now
        for key, bucket in list(self._buckets.items()):
            if now - bucket.updated > 900:
                self._buckets.pop(key, None)


class ConnectionCounter:
    """Caps concurrent websocket connections per IP and per user."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ips: Dict[str, int] = defaultdict(int)
        self._users: Dict[str, int] = defaultdict(int)

    def acquire(self, ip: str, user_id: str) -> Tuple[bool, str]:
        with self._lock:
            if self._ips[ip] >= config.MAX_CONNECTIONS_PER_IP:
                return False, "Too many connections from this network"
            if self._users[user_id] >= config.MAX_CONNECTIONS_PER_USER:
                return False, "Too many simultaneous sessions for this account"
            self._ips[ip] += 1
            self._users[user_id] += 1
            return True, ""

    def release(self, ip: str, user_id: str) -> None:
        with self._lock:
            for store, key in ((self._ips, ip), (self._users, user_id)):
                if store.get(key):
                    store[key] -= 1
                    if store[key] <= 0:
                        store.pop(key, None)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {"ips": len(self._ips), "users": len(self._users)}


http_limiter = HttpRateLimiter()
connection_counter = ConnectionCounter()


def client_ip(request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]
