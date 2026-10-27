"""Signing in with Discord: the OAuth2 authorization code flow.

The rules that keep it safe:

- Every flow starts on this server. It stores a random, single-use `state` that expires after ten minutes and binds it
  to a random HttpOnly cookie in the browser that started it. A callback without both is refused before Discord is
  asked anything, so another site cannot log a visitor in as someone else (login CSRF) or get its own Discord account
  linked to a visitor's account.
- The redirect address comes from the settings (site_url), never from the request's Host header.
- The client secret stays on the server. Discord's tokens are used for two requests, then revoked and forgotten.
- Discord accounts are matched by their numeric id only. A Discord account is never attached to an existing account
  because a name matches; linking happens only for the person signed in, in a flow they started.
- Disabled users cannot sign in this way either. Accounts created through Discord are regular users, and only when an
  admin allows it; an admin can also require membership in one Discord server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__, db, settings as app_settings
from .config import CONFIG
from .security import one_alphabet

log = logging.getLogger(__name__)

STATE_TTL = 10 * 60
MAX_PENDING = 500
STATE_COOKIE = "apm_oauth"
CALLBACK_PATH = "/api/auth/discord/callback"
# Where the browser may be sent after signing in: pages of this app only, never another site
_SAFE_NEXT = re.compile(r"^#/[A-Za-z0-9/_\-]*$")
# Discord's image server, and what a picture's name there looks like (a_ for an animated one)
AVATAR_HOST = "https://cdn.discordapp.com"
_AVATAR = re.compile(r"(a_)?[0-9a-f]{32}")


class DiscordError(Exception):
    """A failed sign-in. `code` is one of a fixed set the web UI turns into a message; it never carries input."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


@dataclass
class _Pending:
    browser_hash: str
    mode: str  # "login" or "link"
    user_id: int | None
    next: str
    created: float


_pending: dict[str, _Pending] = {}
_lock = threading.Lock()  # routes run in worker threads


def redirect_uri() -> str:
    return app_settings.get_setting("site_url") + CALLBACK_PATH


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def begin(mode: str, *, user_id: int | None = None, next_page: str = "") -> tuple[str, str]:
    """Starts a flow. Returns the Discord address to send the browser to and the value for the state cookie."""
    cfg = app_settings.get_settings()
    if not app_settings.discord_login_ready(cfg):
        raise DiscordError("disabled")
    now = time.time()
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with _lock:
        for key in [k for k, p in _pending.items() if now - p.created > STATE_TTL]:
            del _pending[key]
        while len(_pending) >= MAX_PENDING:  # a flood of starts only pushes out the oldest unfinished ones
            del _pending[next(iter(_pending))]
        _pending[state] = _Pending(_hash(browser), mode, user_id, safe_next(next_page), now)
    scopes = "identify guilds" if cfg["discord_guild_id"] else "identify"
    query = urllib.parse.urlencode({"response_type": "code", "client_id": cfg["discord_client_id"], "scope": scopes,
                                    "state": state, "redirect_uri": redirect_uri(), "prompt": "none"})
    return f"{CONFIG.discord_authorize}?{query}", browser


def take(state: str | None, browser: str | None) -> _Pending:
    """The flow a callback belongs to, used up. Raises DiscordError("expired") for anything that does not match."""
    with _lock:
        pending = _pending.pop(state or "", None)
    if pending is None or not browser or time.time() - pending.created > STATE_TTL:
        raise DiscordError("expired")
    if not hmac.compare_digest(pending.browser_hash, _hash(browser)):
        # The state is spent now: someone who saw it in an address cannot try it from their own browser either.
        raise DiscordError("expired")
    return pending


# ----------------------------------------------------------------------------------------------------------------------
# talking to Discord
# ----------------------------------------------------------------------------------------------------------------------
def _request(path: str, *, data: dict | None = None, token: str | None = None) -> object:
    headers = {"User-Agent": f"DiscordBot (https://github.com/NiklaskoDev/ArchipelagoManagerDocker, {__version__})",
               "Accept": "application/json"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{CONFIG.discord_api}{path}", data=body, headers=headers,
                                     method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(1024 * 1024)
    except urllib.error.HTTPError as e:
        raise DiscordError("discord", f"Discord answered {e.code} to {path}") from None
    except (urllib.error.URLError, OSError) as e:
        raise DiscordError("unreachable", f"Could not reach Discord: {getattr(e, 'reason', e)}") from None
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        raise DiscordError("discord", f"Discord sent something unexpected for {path}") from None


def fetch_identity(code: str) -> dict:
    """Swaps the one-time code for the Discord account behind it: {"id", "username", "name", "avatar"}. Checks server
    membership if required."""
    cfg = app_settings.get_settings()
    grant = _request("/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": cfg["discord_client_id"], "client_secret": cfg["discord_client_secret"]})
    token = grant.get("access_token") if isinstance(grant, dict) else None
    if not isinstance(token, str) or not token:
        raise DiscordError("discord", "Discord did not hand out a token")
    try:
        me = _request("/users/@me", token=token)
        if not isinstance(me, dict) or not re.fullmatch(r"\d{5,25}", str(me.get("id", ""))):
            raise DiscordError("discord", "Discord sent no usable account id")
        if cfg["discord_guild_id"]:
            guilds = _request("/users/@me/guilds", token=token)
            if not isinstance(guilds, list) or not any(str(g.get("id")) == cfg["discord_guild_id"]
                                                       for g in guilds if isinstance(g, dict)):
                raise DiscordError("not_member")
    finally:
        try:  # the token is not needed again; do not leave it valid
            _request("/oauth2/token/revoke", data={"token": token, "client_id": cfg["discord_client_id"],
                                                   "client_secret": cfg["discord_client_secret"]})
        except DiscordError as e:
            log.info("Revoking a Discord token failed: %s", e)
    name = str(me.get("global_name") or me.get("username") or "")[:64]
    avatar = me.get("avatar")  # only ever used as part of an address on Discord's image server, so checked strictly
    return {"id": str(me["id"]), "username": str(me.get("username") or ""), "name": name,
            "avatar": avatar if isinstance(avatar, str) and _AVATAR.fullmatch(avatar) else ""}


def avatar_url(row: sqlite3.Row) -> str | None:
    """The picture of a user's linked Discord account, at twice the size the top bar shows it. Discord's default
    picture for an account without one of its own; None while it is not known (linked before the manager kept it)."""
    if not row["discord_id"] or row["discord_avatar"] is None:
        return None
    if row["discord_avatar"]:
        return f"{AVATAR_HOST}/avatars/{row['discord_id']}/{row['discord_avatar']}.png?size=64"
    return f"{AVATAR_HOST}/embed/avatars/{(int(row['discord_id']) >> 22) % 6}.png"


# ----------------------------------------------------------------------------------------------------------------------
# accounts
# ----------------------------------------------------------------------------------------------------------------------
def user_for(discord_id: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM users WHERE discord_id = ?", (discord_id,))


def link(user_id: int, identity: dict) -> None:
    other = user_for(identity["id"])
    if other is not None and other["id"] != user_id:
        raise DiscordError("taken")
    db.execute("UPDATE users SET discord_id = ?, discord_name = ?, discord_avatar = ? WHERE id = ?",
               (identity["id"], identity["name"], identity["avatar"], user_id))


def _free_username(wanted: str) -> str:
    base = re.sub(r"[^\w. -]", "", unicodedata.normalize("NFC", wanted)).strip()[:28].strip()
    if len(base) < 2 or not any(c.isalnum() for c in base) or not one_alphabet(base):  # the rules of check_username
        base = "player"
    name, counter = base, 2
    while db.query_one("SELECT 1 FROM users WHERE username = ?", (name,)):
        name, counter = f"{base}-{counter}", counter + 1
    return name


def sign_up(identity: dict) -> int:
    """A new regular user for a Discord account nobody linked yet. Never takes over an existing name."""
    for _attempt in range(5):
        username = _free_username(identity["username"] or identity["name"])
        try:
            # An empty password hash matches no password: the account signs in with Discord until a password is set.
            return db.execute("INSERT INTO users(username, password_hash, role, created_at, discord_id, discord_name, "
                              "discord_avatar) VALUES (?, '', 'user', ?, ?, ?, ?)",
                              (username, time.time(), identity["id"], identity["name"], identity["avatar"]))
        except sqlite3.IntegrityError:  # the same name or Discord account was taken a moment ago
            existing = user_for(identity["id"])
            if existing is not None:
                return existing["id"]
    raise DiscordError("discord", "Could not find a free username")


def safe_next(value: str) -> str:
    """A page of this app to continue on; anything else (another site, odd characters) becomes the start page."""
    return value if _SAFE_NEXT.match(value or "") else "#/"
