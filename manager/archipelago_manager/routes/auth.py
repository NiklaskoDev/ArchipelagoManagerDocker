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

from .. import __version__, audit, db, discord_auth, invites, limits, notify, settings as app_settings
from ..config import CONFIG
from ..security import (ROLES, SESSION_COOKIE, LoginThrottle, admin_user, check_password_policy, check_username,
                        client_address, create_session, current_user, destroy_session, destroy_user_sessions,
                        hash_password, https_in_use, is_organizer, login_throttle, normal_username, organizer_user,
                        revoke_user_tokens, set_session_cookie, user_for_request, verify_password)
from ..security import ip_throttle

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)


def user_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "disabled": bool(row["disabled"]),
            "created_at": row["created_at"], "last_login": row["last_login"],
            "discord_name": row["discord_name"] if row["discord_id"] else None,
            "discord_avatar": discord_auth.avatar_url(row),
            "discord_pings": db.jloads(row["discord_pings"], []),
            "has_password": bool(row["password_hash"])}


def _client_key(request: Request, username: str) -> str:
    # the name as the lookup sees it, so "admin " counts as "admin"
    return f"{client_address(request)}|{username.strip().casefold()}"


def has_users() -> bool:
    return db.query_one("SELECT 1 FROM users LIMIT 1") is not None


def create_user(username: str, password: str, role: str, invite_id: int | None = None) -> int:
    username = check_username(username)
    check_password_policy(password)
    if db.query_one("SELECT 1 FROM users WHERE username = ?", (username,)):
        raise HTTPException(409, "That username is already taken")
    return db.execute("INSERT INTO users(username, password_hash, role, created_at, invite_id) VALUES (?,?,?,?,?)",
                      (username, hash_password(password), role, time.time(), invite_id))


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
        # what a player may have of the games they generate (limits.py); None for organizers and administrators
        "limits": limits.summary(user) if user else None,
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
    ip_key = f"ip|{client_address(request)}"
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
    ip_key = f"ip|{client_address(request)}"
    if login_throttle.blocked(key) or ip_throttle.blocked(ip_key):
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")
    row = db.query_one("SELECT * FROM users WHERE username = ?", (normal_username(creds.username),))
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
    # Whoever knew the old password may have made tokens with it, which would outlast the change
    revoke_tokens: bool = True


@router.post("/auth/password")
def change_password(body: PasswordChange, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    # Accounts made through Discord have no password yet and may set a first one; everyone else proves the old one.
    if user["password_hash"] and not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(403, "Current password is wrong")
    check_password_policy(body.new_password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.new_password), user["id"]))
    invites.drop_resets(user["id"])
    destroy_user_sessions(user["id"], except_token=request.cookies.get(SESSION_COOKIE))
    return {"ok": True, "tokens_revoked": revoke_user_tokens(user["id"]) if body.revoke_tokens else 0}


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
    key = client_address(request)
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
        if user is None and pending.mode == "invite":
            user_id = _join_with_discord(pending.invite_id, identity)
            fragment = "#/?discord=welcome"
        elif user is None:
            if not app_settings.get_setting("discord_signup"):
                raise discord_auth.DiscordError("not_linked")
            user_id = discord_auth.sign_up(identity)
            fragment = "#/?discord=welcome"
        elif user["disabled"]:
            raise discord_auth.DiscordError("disabled_user")
        else:
            user_id = user["id"]
            db.execute("UPDATE users SET discord_name = ?, discord_avatar = ? WHERE id = ?",
                       (identity["name"], identity["avatar"], user_id))
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
    db.execute("UPDATE users SET discord_id = NULL, discord_name = NULL, discord_avatar = NULL WHERE id = ?",
               (user["id"],))
    return user_json(db.query_one("SELECT * FROM users WHERE id = ?", (user["id"],)))


def _join_with_discord(invite_id: int | None, identity: dict) -> int:
    """A new account for a Discord account, with the role of the invite the flow started with. (A Discord account that
    already has one signs in with it in discord_callback, and the invite keeps its use.)"""
    invite = invites.take_use(invite_id) if invite_id else None
    if invite is None:
        raise discord_auth.DiscordError("invite")
    try:
        user_id = discord_auth.sign_up(identity, role=invite["role"], invite_id=invite["id"])
    except BaseException:
        invites.give_back(invite["id"])
        raise
    _joined(user_id, invite)
    return user_id


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
# invite and reset links (invites.py). Their token comes in the body of a request, never in an address, which would end
# up in logs; a wrong one counts as a failed login of the address, so nobody can try many.
# ----------------------------------------------------------------------------------------------------------------------
class LinkToken(BaseModel):
    token: str = Field(min_length=1, max_length=128)


def _link_guard(request: Request) -> str:
    ip_key = f"ip|{client_address(request)}"
    if ip_throttle.blocked(ip_key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    return ip_key


def _usable_invite(request: Request, token: str) -> sqlite3.Row:
    ip_key = _link_guard(request)
    invite = invites.find(token)
    if invite is None:
        ip_throttle.fail(ip_key)
        raise HTTPException(410, "This invite link cannot be used any more: it expired, was used up or was revoked. "
                                 "Ask whoever sent it for a new one.")
    return invite


def _joined(user_id: int, invite: sqlite3.Row) -> None:
    """The admins' log: who joined with which invite (nobody was signed in to make the request)."""
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    audit.record(row, "user.join", ("user", user_id, row["username"]),
                 {"role": invite["role"], **({"invite": invite["note"]} if invite["note"] else {})})


@router.post("/auth/invite")
def invite_info(body: LinkToken, request: Request) -> dict:
    """What an invite link gives, for the page it opens."""
    invite = _usable_invite(request, body.token)
    return {"role": invite["role"], "note": invite["note"], "invited_by": invite["creator"],
            "expires_at": invite["expires_at"], "discord": app_settings.discord_login_ready(app_settings.get_settings())}


class InviteJoin(LinkToken):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@router.post("/auth/invite/join")
def invite_join(body: InviteJoin, request: Request, response: Response) -> dict:
    """An account made with an invite link, also while registration is closed. Signs it in."""
    invite = _usable_invite(request, body.token)
    if invites.take_use(invite["id"]) is None:  # the last use went a moment ago
        raise HTTPException(410, "This invite link cannot be used any more: it expired, was used up or was revoked. "
                                 "Ask whoever sent it for a new one.")
    try:
        user_id = create_user(body.username, body.password, invite["role"], invite_id=invite["id"])
    except BaseException:  # a name that is taken, a password too short: the use is not spent
        invites.give_back(invite["id"])
        raise
    _joined(user_id, invite)
    return _login_response(request, response, user_id)


@router.post("/auth/invite/discord")
def invite_discord(body: LinkToken, request: Request, response: Response) -> dict:
    """Starts making an account with an invite link through Discord. The browser then goes to the returned address;
    the invite's use is taken when Discord sends it back (discord_callback)."""
    invite = _usable_invite(request, body.token)
    key = client_address(request)
    if _discord_starts.blocked(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    _discord_starts.fail(key)
    try:
        url, browser = discord_auth.begin("invite", invite_id=invite["id"])
    except discord_auth.DiscordError:
        raise HTTPException(409, "Signing in with Discord is not set up on this server") from None
    _state_cookie(request, response, browser)
    return {"url": url}


@router.post("/auth/reset")
def reset_info(body: LinkToken, request: Request) -> dict:
    """Whose password a reset link sets, for the page it opens."""
    ip_key = _link_guard(request)
    user = invites.find_reset(body.token)
    if user is None:
        ip_throttle.fail(ip_key)
        raise HTTPException(410, "This link to set a new password cannot be used any more: it expired or was used. "
                                 "Ask an administrator for a new one.")
    return {"username": user["username"]}


class ResetUse(LinkToken):
    password: str = Field(max_length=256)


@router.post("/auth/reset/use")
def reset_use(body: ResetUse, request: Request, response: Response) -> dict:
    """Sets the new password, once. Signs the user out everywhere else and revokes their API tokens, as whoever knew
    the old password may have made some; then signs them in."""
    ip_key = _link_guard(request)
    check_password_policy(body.password)
    user_id = invites.use_reset(body.token, hash_password(body.password))
    if user_id is None:
        ip_throttle.fail(ip_key)
        raise HTTPException(410, "This link to set a new password cannot be used any more: it expired or was used. "
                                 "Ask an administrator for a new one.")
    destroy_user_sessions(user_id)
    revoked = revoke_user_tokens(user_id)
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    audit.record(row, "user.password_reset", ("user", user_id, row["username"]),
                 {"tokens_revoked": revoked} if revoked else None)
    return {**_login_response(request, response, user_id), "tokens_revoked": revoked}


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
            invites.drop_resets(user_id)
    if body.password:  # for someone who lost theirs, or whose account someone else used: they start afresh
        check_password_policy(body.password)
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.password), user_id))
        destroy_user_sessions(user_id)
        revoke_user_tokens(user_id)
        invites.drop_resets(user_id)
    if body.unlink_discord:
        db.execute("UPDATE users SET discord_id = NULL, discord_name = NULL, discord_avatar = NULL WHERE id = ?",
                   (user_id,))
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


class NewInvite(BaseModel):
    role: str = "user"
    max_uses: int = 1
    days: int = 7
    note: str = Field(default="", max_length=200)


@router.get("/users/invites")
def list_invites(_admin: sqlite3.Row = Depends(admin_user)) -> list[dict]:
    return invites.listing()


@router.post("/users/invites")
def add_invite(body: NewInvite, request: Request, admin: sqlite3.Row = Depends(admin_user)) -> dict:
    """A new invite link. Its token is shown this once: only a hash of it is kept."""
    try:
        token, invite_id = invites.create(admin, body.role, body.max_uses, body.days, body.note)
    except invites.InviteError as e:
        raise HTTPException(e.status, e.message) from None
    invite = next(i for i in invites.listing() if i["id"] == invite_id)
    audit.note(request, target=("invite", invite_id, invite["note"]), role=body.role, uses=body.max_uses, days=body.days)
    return {"token": token, "invite": invite}


@router.delete("/users/invites/{invite_id}")
def delete_invite(invite_id: int, request: Request, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    """Revokes an invite link. The accounts made with it stay."""
    row = invites.delete(invite_id)
    if row is None:
        raise HTTPException(404, "No such invite")
    audit.note(request, target=("invite", invite_id, row["note"]))
    return {"ok": True}


@router.post("/users/{user_id}/reset-link")
def reset_link(user_id: int, request: Request, admin: sqlite3.Row = Depends(admin_user)) -> dict:
    """A link with which a user sets a new password, once, within a day; it replaces one they had. Its token is shown
    this once."""
    target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(404, "No such user")
    if target["disabled"]:
        raise HTTPException(409, "Enable the account first: a disabled account cannot set a password")
    token, expires_at = invites.create_reset(user_id, admin)
    audit.note(request, target=("user", user_id, target["username"]))
    return {"token": token, "expires_at": expires_at}
