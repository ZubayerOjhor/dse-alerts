"""Passwords and sessions for the web UI.

* Passwords are hashed with argon2id. Nothing else is ever stored.
* A session is a signed cookie carrying the user id, a CSRF token and a short fingerprint of
  the password hash; it is HttpOnly, SameSite=Lax and Secure over HTTPS. Changing the password
  invalidates every other session. There is no server-side session store to leak.
* Login and registration are rate limited per client address and per email, in memory.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

_ph = PasswordHasher()
SESSION_COOKIE = "dse_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14   # two weeks
PASSWORD_MAX_LENGTH = 128             # argon2 cost grows with input size; nobody needs more


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    if not hashed or len(password) > PASSWORD_MAX_LENGTH:
        return False
    try:
        return _ph.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


class Sessions:
    def __init__(self, secret_key: str):
        self._s = URLSafeTimedSerializer(secret_key, salt="dse-session")

    def issue(self, user_id: int, password_hash: str | None = None) -> str:
        return self._s.dumps({"uid": user_id, "csrf": secrets.token_urlsafe(16), "pw": password_tag(password_hash)})

    def read(self, token: str | None) -> dict | None:
        if not token:
            return None
        try:
            sess = self._s.loads(token, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None
        return sess if isinstance(sess, dict) and "uid" in sess and "csrf" in sess else None


def password_tag(password_hash: str | None) -> str:
    """A short, non-reversible fingerprint of the stored hash; sessions carry it so that a
    password change logs every other browser out."""
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:12]


class RateLimiter:
    """Sliding-window counter per key, in memory. Enough for one instance behind a proxy."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Record one attempt for ``key`` and return True when it is over the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - window_seconds:
                q.popleft()
            q.append(now)
            if len(self._hits) > 10000:   # bound memory under a flood of distinct keys
                for k in [k for k, v in self._hits.items() if not v or v[-1] < now - window_seconds]:
                    del self._hits[k]
            return len(q) > limit

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
