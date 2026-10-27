"""Password hashing, cookie sessions, API tokens and request guards."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import time
import unicodedata
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, Response

from . import db, settings as app_settings

SESSION_COOKIE = "apm_session"
SESSION_TTL = 30 * 24 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "archipelago-manager"

# Personal API tokens: "Authorization: Bearer apm_..." instead of the session cookie (routes/tokens.py)
TOKEN_PREFIX = "apm_"
TOKEN_TOUCH_INTERVAL = 60  # how often a token's "last used" is written, at most
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

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


def normal_username(username: str) -> str:
    """A username as stored and looked up: without surrounding spaces, and é typed as e + ´ the same as é (NFC)."""
    return unicodedata.normalize("NFC", username.strip())


def check_username(username: str) -> str:
    username = normal_username(username)
    if not (2 <= len(username) <= 32):
        raise HTTPException(400, "Username must be 2-32 characters long")
    if not all(c.isalnum() or c in "._- " for c in username):
        raise HTTPException(400, "Username may only contain letters, numbers, spaces and . _ -")
    if not any(c.isalnum() for c in username):  # ".." or "- -" would be hard to tell apart and to talk about
        raise HTTPException(400, "A username needs at least one letter or digit")
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


def revoke_user_tokens(user_id: int) -> int:
    """Revokes all API tokens of a user. Returns how many there were."""
    with db.connection() as conn:
        return conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,)).rowcount


def https_in_use(request: Request) -> bool:
    """Whether cookies should be marked secure: this request came over https, or the web UI's address is https (behind
    a reverse proxy whose X-Forwarded-Proto is not trusted, the request itself looks like plain http)."""
    return request.url.scheme == "https" or app_settings.get_setting("site_url").startswith("https://")


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=https_in_use(request), path="/")


def bearer_token(request: Request) -> str | None:
    """The API token a request carries, if any. Other Authorization headers (a reverse proxy's basic auth) are not
    ours and are left alone."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    value = value.strip()
    return value if scheme.lower() == "bearer" and value.startswith(TOKEN_PREFIX) else None


def new_api_token() -> tuple[str, str]:
    """A new token and the hash that is stored for it."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, _hash_token(token)


def token_for_request(request: Request) -> dict | None:
    """What the request's API token is (id, name, access), once user_for_request has accepted it."""
    return request.scope.get("api_token")


def _user_for_token(request: Request, token: str) -> sqlite3.Row | None:
    now = time.time()
    found = db.query_one("SELECT id, user_id, name, access, last_used_at FROM api_tokens "
                         "WHERE token_hash = ? AND (expires_at IS NULL OR expires_at > ?)", (_hash_token(token), now))
    if not found:
        return None
    user = db.query_one("SELECT * FROM users WHERE id = ? AND disabled = 0", (found["user_id"],))
    if user is None:
        return None
    request.scope["api_token"] = {"id": found["id"], "name": found["name"], "access": found["access"]}
    if not found["last_used_at"] or now - found["last_used_at"] > TOKEN_TOUCH_INTERVAL:
        db.execute("UPDATE api_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
                   (now, request.client.host if request.client else None, found["id"]))
    return user


def user_for_request(request: Request) -> sqlite3.Row | None:
    """The signed-in user: from an API token if the request carries one (then only from it), from the session cookie
    otherwise."""
    if "api_token_user" in request.scope:  # looked up once per request (the middlewares and the route all ask)
        return request.scope["api_token_user"]
    api_token = bearer_token(request)
    if api_token is not None:
        user = _user_for_token(request, api_token)
        request.scope["api_token_user"] = user
        return user
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


# Organizers run sign-up rounds and the games they add or generate, as administrators do, but do not administer the
# server: users, settings, custom games, ROMs and Archipelago versions stay with the administrators.
ROLES = ("admin", "organizer", "user")


def is_organizer(user: sqlite3.Row) -> bool:
    """Whether a user may add games and open sign-up rounds (administrators may too)."""
    return user["role"] in ("admin", "organizer")


def organizer_user(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if not is_organizer(user):
        raise HTTPException(403, "Only organizers and administrators can do this")
    return user


def manages(row: sqlite3.Row, user: sqlite3.Row) -> bool:
    """Whether a user runs a game or a sign-up round (created_by): administrators run all of them, organizers their own."""
    return is_admin(user) or (user["role"] == "organizer" and row["created_by"] == user["id"])


def client_address(request: Request) -> str:
    """Who a request comes from, as the limits on failed attempts count them: the IP address, but for IPv6 its /64
    network. One home connection gets a whole /64, so trying from the next address of it buys no more guesses."""
    host = request.client.host if request.client else "?"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6:
        return str(address.ipv4_mapped) if address.ipv4_mapped else str(ipaddress.ip_network(f"{address}/64", strict=False))
    return host


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
        if len(self._failures) > 10000:  # many different keys: forget the ones whose failures are all old
            for old in list(self._failures):
                self._prune(old)
        self._failures[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


login_throttle = LoginThrottle()          # per client address and username
ip_throttle = LoginThrottle(limit=40)     # per client address, so rotating through usernames does not help
