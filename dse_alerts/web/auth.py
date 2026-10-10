"""Passwords and sessions for the web UI.

* Passwords are hashed with argon2id. Nothing else is ever stored.
* A session is a signed cookie carrying the user id and a CSRF token; it is HttpOnly and
  SameSite=Lax. There is no server-side session store to leak.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

_ph = PasswordHasher()
SESSION_COOKIE = "dse_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14   # two weeks


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    if not hashed:
        return False
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError:
        return False


class Sessions:
    def __init__(self, secret_key: str):
        self._s = URLSafeTimedSerializer(secret_key, salt="dse-session")

    def issue(self, user_id: int) -> str:
        return self._s.dumps({"uid": user_id, "csrf": secrets.token_urlsafe(16)})

    def read(self, token: str | None) -> dict | None:
        if not token:
            return None
        try:
            return self._s.loads(token, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None
