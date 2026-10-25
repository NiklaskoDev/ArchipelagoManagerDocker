"""Personal API tokens: for scripts and bots that use this API as the user who made the token.

A request with "Authorization: Bearer <token>" acts as the token's owner, with the owner's rights: everything (write)
or only GET requests (read). Tokens are made, listed and revoked here with the session cookie only (main.guard), so a
token cannot make itself new ones. Only a hash of a token is stored: it is shown once, when it is made.
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db
from ..security import current_user, is_admin, new_api_token

router = APIRouter(prefix="/api")

MAX_TOKENS_PER_USER = 25
MAX_DAYS = 3650


def token_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "hint": row["hint"], "access": row["access"],
            "created_at": row["created_at"], "expires_at": row["expires_at"], "last_used_at": row["last_used_at"],
            "last_used_ip": row["last_used_ip"], "expired": bool(row["expires_at"] and row["expires_at"] <= time.time()),
            "user_id": row["user_id"], "username": row["username"]}


_SELECT = "SELECT t.*, u.username FROM api_tokens t JOIN users u ON u.id = t.user_id"


@router.get("/tokens")
def list_tokens(all: bool = False, user: sqlite3.Row = Depends(current_user)) -> list[dict]:  # noqa: A002
    """Your own tokens; with ?all=true, an administrator gets everybody's."""
    if all and is_admin(user):
        rows = db.query(f"{_SELECT} ORDER BY u.username COLLATE NOCASE, t.id DESC")
    else:
        rows = db.query(f"{_SELECT} WHERE t.user_id = ? ORDER BY t.id DESC", (user["id"],))
    return [token_json(r) for r in rows]


class TokenCreate(BaseModel):
    name: str = Field(max_length=200)
    access: str = "read"  # read: GET requests only; write: everything the user may do
    expires_days: int | None = None  # None: never


@router.post("/tokens")
def create_token(body: TokenCreate, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    name = " ".join(body.name.split())
    if not name or len(name) > 60:
        raise HTTPException(400, "Give the token a name of up to 60 characters, such as what uses it")
    if body.access not in ("read", "write"):
        raise HTTPException(400, "Access must be read or write")
    if body.expires_days is not None and not 1 <= body.expires_days <= MAX_DAYS:
        raise HTTPException(400, f"A token can last between 1 and {MAX_DAYS} days, or forever")
    if db.query_one("SELECT COUNT(*) AS n FROM api_tokens WHERE user_id = ?", (user["id"],))["n"] >= MAX_TOKENS_PER_USER:
        raise HTTPException(409, f"You have {MAX_TOKENS_PER_USER} tokens already. Revoke one you no longer use.")
    token, token_hash = new_api_token()
    now = time.time()
    token_id = db.execute("INSERT INTO api_tokens(user_id, name, token_hash, hint, access, created_at, expires_at) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (user["id"], name, token_hash, token[-4:], body.access, now,
                           now + body.expires_days * 86400 if body.expires_days else None))
    audit.note(request, name=name, access=body.access)
    # the only time the token itself is shown
    return {**token_json(db.query_one(f"{_SELECT} WHERE t.id = ?", (token_id,))), "token": token}


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = db.query_one(f"{_SELECT} WHERE t.id = ?", (token_id,))
    if not row or (row["user_id"] != user["id"] and not is_admin(user)):
        raise HTTPException(404, "No such token")
    db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
    audit.note(request, name=row["name"], owner=row["username"])
    return {"ok": True}
