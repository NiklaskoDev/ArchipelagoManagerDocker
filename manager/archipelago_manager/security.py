"""Password hashing, cookie sessions and request guards."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, Response

from . import db

SESSION_COOKIE = "apm_session"
SESSION_TTL = 30 * 24 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "archipelago-manager"

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(base64.b64decode(expected)))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    if len(password) > 256:
        raise HTTPException(400, "Password is too long")


def check_username(username: str) -> str:
    username = username.strip()
    if not (2 <= len(username) <= 32):
        raise HTTPException(400, "Username must be 2-32 characters long")
    if not all(c.isalnum() or c in "._- " for c in username):
        raise HTTPException(400, "Username may only contain letters, numbers, spaces and . _ -")
    return username


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, user_agent) VALUES (?,?,?,?,?)",
               (_hash_token(token), user_id, now, now + SESSION_TTL, (user_agent or "")[:200]))
    db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def destroy_session(token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def destroy_user_sessions(user_id: int, except_token: str | None = None) -> None:
    keep = _hash_token(except_token) if except_token else ""
    db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep))


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")


def user_for_request(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.query_one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0", (_hash_token(token), time.time()))
    return row


def current_user(request: Request) -> sqlite3.Row:
    user = user_for_request(request)
    if user is None:
        raise HTTPException(401, "Not logged in")
    return user


def admin_user(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


def is_admin(user: sqlite3.Row) -> bool:
    return user["role"] == "admin"


class LoginThrottle:
    """Allows `limit` failed logins per key within `window` seconds."""

    def __init__(self, limit: int = 8, window: float = 15 * 60) -> None:
        self.limit, self.window = limit, window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        events = self._failures[key]
        cutoff = time.monotonic() - self.window
        while events and events[0] < cutoff:
            events.popleft()
        if not events:
            self._failures.pop(key, None)
        return events

    def blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._failures[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


login_throttle = LoginThrottle()          # per client address and username
ip_throttle = LoginThrottle(limit=40)     # per client address, so rotating through usernames does not help
