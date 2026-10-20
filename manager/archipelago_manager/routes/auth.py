"""Login, registration and user administration."""
from __future__ import annotations

import shutil
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import __version__, db, settings as app_settings
from ..config import CONFIG
from ..security import (SESSION_COOKIE, admin_user, check_password_policy, check_username, create_session,
                        current_user, destroy_session, destroy_user_sessions, hash_password, login_throttle,
                        set_session_cookie, user_for_request, verify_password)
from ..security import ip_throttle

router = APIRouter(prefix="/api")


def user_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "disabled": bool(row["disabled"]),
            "created_at": row["created_at"], "last_login": row["last_login"]}


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "?"
    return f"{host}|{username.lower()}"


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
        "user": user_json(user) if user else None,
        "permissions": {
            "start_rooms": cfg["users_can_start_rooms"],
            "generate": cfg["users_can_generate"],
            "upload_apworlds": cfg["users_can_upload_apworlds"],
        },
    }


def _login_response(request: Request, response: Response, user_id: int) -> dict:
    token = create_session(user_id, request.headers.get("user-agent"))
    set_session_cookie(request, response, token)
    return user_json(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


@router.post("/auth/setup")
def setup(creds: Credentials, request: Request, response: Response) -> dict:
    if has_users():
        raise HTTPException(409, "Setup has already been completed")
    user_id = create_user(creds.username, creds.password, "admin")
    return _login_response(request, response, user_id)


@router.post("/auth/login")
def login(creds: Credentials, request: Request, response: Response) -> dict:
    key = _client_key(request, creds.username)
    ip_key = f"ip|{request.client.host if request.client else '?'}"
    if login_throttle.blocked(key) or ip_throttle.blocked(ip_key):
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")
    row = db.query_one("SELECT * FROM users WHERE username = ?", (creds.username.strip(),))
    # Verify even for unknown users so response time does not reveal which usernames exist.
    stored = row["password_hash"] if row else "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    valid = verify_password(creds.password, stored)
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
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/auth/password")
def change_password(body: PasswordChange, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(403, "Current password is wrong")
    check_password_policy(body.new_password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.new_password), user["id"]))
    destroy_user_sessions(user["id"], except_token=request.cookies.get(SESSION_COOKIE))
    return {"ok": True}


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


class NewUser(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    role: str = "user"


@router.post("/users")
def add_user(body: NewUser, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    if body.role not in ("admin", "user"):
        raise HTTPException(400, "Role must be 'admin' or 'user'")
    user_id = create_user(body.username, body.password, body.role)
    return user_json(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, max_length=256)


def _active_admin_count(excluding: int) -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                        (excluding,))["n"]


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, admin: sqlite3.Row = Depends(admin_user)) -> dict:
    target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(404, "No such user")
    demotes = (body.role == "user" and target["role"] == "admin") or (body.disabled is True and target["role"] == "admin")
    if demotes and _active_admin_count(excluding=user_id) == 0:
        raise HTTPException(409, "There must be at least one active administrator")
    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(400, "Role must be 'admin' or 'user'")
        db.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
    if body.disabled is not None:
        db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), user_id))
        if body.disabled:
            destroy_user_sessions(user_id)
    if body.password:
        check_password_policy(body.password)
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.password), user_id))
        destroy_user_sessions(user_id)
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
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    shutil.rmtree(CONFIG.configs_dir / str(user_id), ignore_errors=True)
    return {"ok": True}
