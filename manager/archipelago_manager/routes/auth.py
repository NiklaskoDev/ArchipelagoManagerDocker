"""Login, registration and user administration."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .. import __version__, audit, db, discord_auth, notify, settings as app_settings
from ..config import CONFIG
from ..security import (ROLES, SESSION_COOKIE, LoginThrottle, admin_user, check_password_policy, check_username,
                        create_session, current_user, destroy_session, destroy_user_sessions, hash_password,
                        https_in_use, is_organizer, login_throttle, organizer_user, set_session_cookie,
                        user_for_request, verify_password)
from ..security import ip_throttle

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)


def user_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "disabled": bool(row["disabled"]),
            "created_at": row["created_at"], "last_login": row["last_login"],
            "discord_name": row["discord_name"] if row["discord_id"] else None,
            "discord_pings": db.jloads(row["discord_pings"], []),
            "has_password": bool(row["password_hash"])}


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "?"
    return f"{host}|{username.strip().casefold()}"  # as the lookup sees the name, so "admin " counts as "admin"


def has_users() -> bool:
    return db.query_one("SELECT 1 FROM users LIMIT 1") is not None


def create_user(username: str, password: str, role: str) -> int:
    username = check_username(username)
    check_password_policy(password)
    if db.query_one("SELECT 1 FROM users WHERE username = ?", (username,)):
        raise HTTPException(409, "That username is already taken")
    return db.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                      (username, hash_password(password), role, time.time()))


def bootstrap_admin() -> None:
    """Creates the first admin from ADMIN_USER / ADMIN_PASSWORD, if configured and no user exists yet."""
    if not has_users() and CONFIG.admin_user and CONFIG.admin_password:
        create_user(CONFIG.admin_user, CONFIG.admin_password, "admin")


# ----------------------------------------------------------------------------------------------------------------------
# session
# ----------------------------------------------------------------------------------------------------------------------
class Credentials(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@router.get("/auth/status")
def status(request: Request) -> dict:
    user = user_for_request(request)
    cfg = app_settings.get_settings()
    return {
        "version": __version__,
        "needs_setup": not has_users(),
        "allow_registration": cfg["allow_registration"],
        # Discord sends people back to site_url, so the sign-in has to start there as well
        "discord_login": app_settings.discord_login_ready(cfg),
        "site_url": cfg["site_url"] or None,
        "user": user_json(user) if user else None,
        "permissions": {
            "start_rooms": cfg["users_can_start_rooms"],
            "generate": cfg["users_can_generate"],
            "upload_apworlds": cfg["users_can_upload_apworlds"],
            # add games, generate and open sign-up rounds, and run those (security.is_organizer)
            "organize": bool(user and is_organizer(user)),
        },
        # what players can ask to be mentioned for on Discord
        "ping_kinds": notify.ping_kinds_offered(cfg) if user else [],
    }


def _login_response(request: Request, response: Response, user_id: int) -> dict:
    token = create_session(user_id, request.headers.get("user-agent"))
    set_session_cookie(request, response, token)
    return user_json(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


# Until there is a first administrator, whoever reaches the web UI could make themselves one. So creating it takes a code
# that only whoever runs the container can read: it is in the container's log and in a file in the data folder. A request
# cannot fake it the way it can fake the address it was sent to.
SETUP_CODE_FILE = "setup-code.txt"
SETUP_CODE_LENGTH = 12
_CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # none that look alike (0 and O, 1 and I)
_setup_code: str | None = None


def setup_code_path() -> Path:
    return CONFIG.data_dir / SETUP_CODE_FILE


def _normalized(code: str) -> str:
    """A code as typed, without the dashes and spaces and in capitals."""
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def _shown(code: str) -> str:
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def prepare_setup() -> None:
    """At startup, while there is no user yet: the setup code. It is made once and stays until it is used, so it does
    not change when the container restarts in between."""
    global _setup_code
    path = setup_code_path()
    if has_users():
        _setup_code = None
        path.unlink(missing_ok=True)
        return
    try:
        code = _normalized(path.read_text(encoding="utf-8"))
    except OSError:
        code = ""
    if len(code) != SETUP_CODE_LENGTH:
        code = "".join(secrets.choice(_CODE_LETTERS) for _ in range(SETUP_CODE_LENGTH))
        path.unlink(missing_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            f.write(_shown(code) + "\n")
    _setup_code = code
    log.warning("There is no administrator yet. Open the web UI and create the account with the setup code %s "
                "(it is also in %s).", _shown(code), path)


class SetupRequest(Credentials):
    code: str = Field(default="", max_length=64)


@router.post("/auth/setup")
def setup(body: SetupRequest, request: Request, response: Response) -> dict:
    global _setup_code
    if has_users():
        raise HTTPException(409, "Setup has already been completed")
    ip_key = f"ip|{request.client.host if request.client else '?'}"
    if ip_throttle.blocked(ip_key):
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")
    if _setup_code is None or not hmac.compare_digest(_normalized(body.code), _setup_code):
        ip_throttle.fail(ip_key)
        raise HTTPException(403, "Enter the setup code. It is in the container's log, and in the file setup-code.txt in "
                                 "the data folder.")
    user_id = create_user(body.username, body.password, "admin")
    _setup_code = None
    setup_code_path().unlink(missing_ok=True)
    return _login_response(request, response, user_id)


@router.post("/auth/login")
def login(creds: Credentials, request: Request, response: Response) -> dict:
    key = _client_key(request, creds.username)
    ip_key = f"ip|{request.client.host if request.client else '?'}"
    if login_throttle.blocked(key) or ip_throttle.blocked(ip_key):
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")
    row = db.query_one("SELECT * FROM users WHERE username = ?", (creds.username.strip(),))
    # Verify even for unknown users and for accounts without a password (Discord only), so the response time does not
    # reveal which usernames exist or how they sign in.
    dummy = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    stored = row["password_hash"] if row and row["password_hash"] else dummy
    valid = verify_password(creds.password, stored) and bool(row and row["password_hash"])
    if not row or not valid or row["disabled"]:
        login_throttle.fail(key)
        ip_throttle.fail(ip_key)
        raise HTTPException(401, "Wrong username or password")
    login_throttle.clear(key)
    return _login_response(request, response, row["id"])


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        destroy_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/register")
def register(creds: Credentials, request: Request, response: Response) -> dict:
    if not app_settings.get_setting("allow_registration"):
        raise HTTPException(403, "Registration is disabled. Ask an administrator for an account.")
    key = _client_key(request, "register")
    if login_throttle.blocked(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    login_throttle.fail(key)  # registrations count against the same budget, so this cannot be used to spam accounts
    user_id = create_user(creds.username, creds.password, "user")
    return _login_response(request, response, user_id)


class PasswordChange(BaseModel):
    current_password: str = Field(default="", max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/auth/password")
def change_password(body: PasswordChange, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    # Accounts made through Discord have no password yet and may set a first one; everyone else proves the old one.
    if user["password_hash"] and not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(403, "Current password is wrong")
    check_password_policy(body.new_password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.new_password), user["id"]))
    destroy_user_sessions(user["id"], except_token=request.cookies.get(SESSION_COOKIE))
    return {"ok": True}


# ----------------------------------------------------------------------------------------------------------------------
# Discord (see discord_auth.py for how the flow is kept safe)
# ----------------------------------------------------------------------------------------------------------------------
def _state_cookie(request: Request, response: Response, value: str) -> None:
    response.set_cookie(discord_auth.STATE_COOKIE, value, max_age=discord_auth.STATE_TTL, httponly=True,
                        samesite="lax", secure=https_in_use(request), path="/api/auth/discord")


def _back_to_app(fragment: str) -> RedirectResponse:
    response = RedirectResponse("/" + fragment, status_code=303)
    response.delete_cookie(discord_auth.STATE_COOKIE, path="/api/auth/discord")
    return response


# Each start keeps a little state until it finishes or expires. Starts per address are limited, so nobody can push
# everyone else's unfinished sign-ins out (discord_auth keeps at most 500).
_discord_starts = LoginThrottle(limit=30, window=10 * 60)


@router.get("/auth/discord/login")
def discord_login(request: Request, next: str = "") -> RedirectResponse:
    """Sends the browser to Discord. Opened as a page (not fetched), on the site_url address."""
    key = request.client.host if request.client else "?"
    if _discord_starts.blocked(key):
        return _back_to_app("#/?discord_error=busy")
    _discord_starts.fail(key)
    try:
        url, browser = discord_auth.begin("login", next_page=next)
    except discord_auth.DiscordError as e:
        return _back_to_app(f"#/?discord_error={e.code}")
    response = RedirectResponse(url, status_code=303)
    _state_cookie(request, response, browser)
    return response


@router.post("/auth/discord/link")
def discord_link(request: Request, response: Response, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Starts linking a Discord account to the signed-in user. The browser then goes to the returned address."""
    try:
        url, browser = discord_auth.begin("link", user_id=user["id"])
    except discord_auth.DiscordError:
        raise HTTPException(409, "Signing in with Discord is not set up on this server") from None
    _state_cookie(request, response, browser)
    return {"url": url}


@router.get("/auth/discord/callback")
async def discord_callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    try:
        # first, before anything else: a callback this browser did not start goes nowhere
        pending = discord_auth.take(state, request.cookies.get(discord_auth.STATE_COOKIE))
        if error or not code or len(code) > 512:
            raise discord_auth.DiscordError("denied")
        identity = await asyncio.to_thread(discord_auth.fetch_identity, code)
        if pending.mode == "link":
            user = user_for_request(request)
            if user is None or user["id"] != pending.user_id:
                raise discord_auth.DiscordError("expired")
            discord_auth.link(user["id"], identity)
            return _back_to_app("#/?discord=linked")
        user = discord_auth.user_for(identity["id"])
        fragment = discord_auth.safe_next(pending.next)
        if user is None:
            if not app_settings.get_setting("discord_signup"):
                raise discord_auth.DiscordError("not_linked")
            user_id = discord_auth.sign_up(identity)
            fragment = "#/?discord=welcome"
        elif user["disabled"]:
            raise discord_auth.DiscordError("disabled_user")
        else:
            user_id = user["id"]
            db.execute("UPDATE users SET discord_name = ? WHERE id = ?", (identity["name"], user_id))
    except discord_auth.DiscordError as e:
        log.info("Discord sign-in refused: %s", e)
        return _back_to_app(f"#/?discord_error={e.code}")
    response = _back_to_app(fragment)
    set_session_cookie(request, response, create_session(user_id, request.headers.get("user-agent")))
    return response


@router.post("/auth/discord/unlink")
def discord_unlink(user: sqlite3.Row = Depends(current_user)) -> dict:
    if not user["password_hash"]:
        raise HTTPException(409, "Set a password first, or you could not sign in any more")
    db.execute("UPDATE users SET discord_id = NULL, discord_name = NULL WHERE id = ?", (user["id"],))
    return user_json(db.query_one("SELECT * FROM users WHERE id = ?", (user["id"],)))


class Pings(BaseModel):
    pings: list[str] = Field(max_length=len(notify.PING_KINDS))


@router.put("/auth/discord/pings")
def discord_pings(body: Pings, user: sqlite3.Row = Depends(current_user)) -> dict:
    """What the signed-in user wants to be mentioned for in the Discord messages."""
    if not user["discord_id"]:
        raise HTTPException(409, "Link your Discord account first")
    if any(kind not in notify.PING_KINDS for kind in body.pings):
        raise HTTPException(400, "Unknown kind of mention")
    pings = [kind for kind in notify.PING_KINDS if kind in body.pings]
    db.execute("UPDATE users SET discord_pings = ? WHERE id = ?", (db.jdumps(pings), user["id"]))
    return user_json(db.query_one("SELECT * FROM users WHERE id = ?", (user["id"],)))


# ----------------------------------------------------------------------------------------------------------------------
# administration
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/users")
def list_users(_admin: sqlite3.Row = Depends(admin_user)) -> list[dict]:
    rows = db.query("""
        SELECT u.*, (SELECT COUNT(*) FROM configs c WHERE c.user_id = u.id) AS config_count,
                    (SELECT COUNT(*) FROM game_slots s WHERE s.user_id = u.id) AS slot_count
        FROM users u ORDER BY u.username COLLATE NOCASE""")
    return [{**user_json(r), "config_count": r["config_count"], "slot_count": r["slot_count"]} for r in rows]


@router.get("/users/names")
def user_names(_user: sqlite3.Row = Depends(organizer_user)) -> list[dict]:
    """Who can be given a slot, for organizers, who do not see the rest of the user list."""
    return [{"id": r["id"], "username": r["username"]}
            for r in db.query("SELECT id, username FROM users WHERE disabled = 0 ORDER BY username COLLATE NOCASE")]


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise HTTPException(400, "Role must be 'admin', 'organizer' or 'user'")


class NewUser(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    role: str = "user"


@router.post("/users")
def add_user(body: NewUser, request: Request, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    _check_role(body.role)
    user_id = create_user(body.username, body.password, body.role)
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    audit.note(request, target=("user", user_id, row["username"]), role=body.role)
    return user_json(row)


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, max_length=256)
    unlink_discord: bool | None = None


def _active_admin_count(excluding: int) -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                        (excluding,))["n"]


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, request: Request, admin: sqlite3.Row = Depends(admin_user)) -> dict:
    target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(404, "No such user")
    demotes = target["role"] == "admin" and ((body.role is not None and body.role != "admin") or body.disabled is True)
    if demotes and _active_admin_count(excluding=user_id) == 0:
        raise HTTPException(409, "There must be at least one active administrator")
    if body.role is not None:
        _check_role(body.role)
        db.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
    if body.disabled is not None:
        db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), user_id))
        if body.disabled:
            destroy_user_sessions(user_id)
    if body.password:
        check_password_policy(body.password)
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.password), user_id))
        destroy_user_sessions(user_id)
    if body.unlink_discord:
        db.execute("UPDATE users SET discord_id = NULL, discord_name = NULL WHERE id = ?", (user_id,))
        destroy_user_sessions(user_id)  # sessions that came from that Discord account end with it
    audit.note(request, **{key: value for key, value in (
        ("role", body.role), ("disabled", body.disabled), ("password", "changed" if body.password else None),
        ("discord", "unlinked" if body.unlink_discord else None)) if value is not None})
    return user_json(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: sqlite3.Row = Depends(admin_user)) -> dict:
    target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(404, "No such user")
    if target["id"] == admin["id"]:
        raise HTTPException(409, "You cannot delete your own account")
    if target["role"] == "admin" and _active_admin_count(excluding=user_id) == 0:
        raise HTTPException(409, "There must be at least one active administrator")
    db.execute("DELETE FROM generation_presets WHERE user_id = ? AND shared = 0", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    # slots offered to them stay with their players (their own slots lose their owner, and so their offers)
    db.execute("UPDATE game_slots SET handover_at = NULL, handover_to = NULL, handover_note = NULL "
               "WHERE handover_to = ? OR user_id IS NULL", (user_id,))
    shutil.rmtree(CONFIG.configs_dir / str(user_id), ignore_errors=True)
    return {"ok": True}
