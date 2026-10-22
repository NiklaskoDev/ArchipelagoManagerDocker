"""Sign-up rounds: administrators open them, players enter configs, the game is generated from all entries."""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import apbridge, db, signups
from ..security import admin_user, current_user, is_admin

router = APIRouter(prefix="/api")


def _error(e: signups.SignupError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.message, "missing_games": e.missing_games})


def _get(signup_id: int) -> sqlite3.Row:
    row = signups.get(signup_id)
    if not row:
        raise HTTPException(404, "No such round")
    return signups.sync(row)


def signup_json(row: sqlite3.Row, user: sqlite3.Row, rows: list[sqlite3.Row] | None = None) -> dict:
    rows = signups.entries(row["id"]) if rows is None else rows
    admin = is_admin(user)
    creator = db.query_one("SELECT username FROM users WHERE id = ?", (row["created_by"],)) if row["created_by"] else None
    return {
        "id": row["id"], "name": row["name"], "description": row["description"], "status": row["status"],
        "open": signups.is_open(row), "deadline": row["deadline"], "created_at": row["created_at"],
        "closed_at": row["closed_at"], "creator": creator["username"] if creator else None,
        "configs_per_user": row["configs_per_user"], "allowed_games": db.jloads(row["allowed_games"], []),
        "auto_generate": bool(row["auto_generate"]), "options": signups.clean_options(db.jloads(row["options"], {})),
        "game_id": row["game_id"], "job_id": row["job_id"] if admin else None,
        "error": row["error"] if admin else None,
        "entry_count": len(rows), "user_count": len({r["user_id"] for r in rows}),
        "my_entry_count": sum(1 for r in rows if r["user_id"] == user["id"]),
    }


def entry_json(row: sqlite3.Row, user: sqlite3.Row, issues: list[str]) -> dict:
    mine = row["user_id"] == user["id"]
    games = db.jloads(row["games"], [])
    result = {
        "id": row["id"], "user_id": row["user_id"], "username": row["username"], "mine": mine,
        "player_names": db.jloads(row["player_names"], []), "games": games,
        "missing_games": apbridge.missing_games(games), "issues": issues,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    if mine or is_admin(user):
        result["name"] = row["name"]
        result["config_id"] = row["config_id"]
        # The library copy was edited after entering: offer to update the entry
        result["outdated"] = bool(row["library_sha256"] and row["library_sha256"] != row["sha256"])
    return result


# ----------------------------------------------------------------------------------------------------------------------
# rounds
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/signups")
def list_signups(user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    rows = [signups.sync(r) for r in db.query("SELECT * FROM signups")]
    # Open rounds first (soonest deadline first), then the rest, newest first
    rows.sort(key=lambda r: (r["status"] != "open", (r["deadline"] or float("inf")) if r["status"] == "open"
                             else -r["created_at"]))
    return [signup_json(r, user) for r in rows]


class SignupBody(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    deadline: float | None = None
    configs_per_user: int | None = None
    allowed_games: list[str] | None = Field(default=None, max_length=200)
    auto_generate: bool | None = None
    options: dict | None = None


def _clean(body: SignupBody, *, creating: bool) -> dict:
    values = body.model_dump(exclude_unset=True)
    if creating or "name" in values:
        name = (values.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Give the round a name")
        values["name"] = name
    if "description" in values:
        values["description"] = (values["description"] or "").strip() or None
    if values.get("deadline") is not None and values["deadline"] <= time.time():
        raise HTTPException(400, "The deadline is in the past")
    if "configs_per_user" in values:
        limit = values["configs_per_user"]
        if limit is None or not 1 <= limit <= signups.MAX_CONFIGS_PER_USER:
            raise HTTPException(400, f"Configs per player must be between 1 and {signups.MAX_CONFIGS_PER_USER}")
    if "auto_generate" in values and values["auto_generate"] is None:
        del values["auto_generate"]
    try:
        if "allowed_games" in values:
            values["allowed_games"] = db.jdumps(signups.clean_allowed_games(values["allowed_games"]))
        if creating or "options" in values:
            values["options"] = db.jdumps(signups.clean_options(values.get("options")))
    except signups.SignupError as e:
        raise HTTPException(e.status, e.message) from None
    if "auto_generate" in values:
        values["auto_generate"] = int(values["auto_generate"])
    return values


@router.post("/signups")
def create_signup(body: SignupBody, user: sqlite3.Row = Depends(admin_user)) -> dict:
    values = _clean(body, creating=True)
    signup_id = db.execute(
        "INSERT INTO signups(name, description, created_by, created_at, deadline, configs_per_user, allowed_games, "
        "auto_generate, options) VALUES (?,?,?,?,?,?,?,?,?)",
        (values["name"], values.get("description"), user["id"], time.time(), values.get("deadline"),
         values.get("configs_per_user", 1), values.get("allowed_games", "[]"), values.get("auto_generate", 1),
         values["options"]))
    signups.announce_open(signup_id)
    return signup_detail(signup_id, user)


@router.get("/signups/{signup_id}")
def signup_detail(signup_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = _get(signup_id)
    rows = signups.entries(signup_id)
    issues = signups.entry_issues(row, rows)
    return {**signup_json(row, user, rows), "entries": [entry_json(r, user, issues[r["id"]]) for r in rows]}


@router.patch("/signups/{signup_id}")
def update_signup(signup_id: int, body: SignupBody, user: sqlite3.Row = Depends(admin_user)) -> dict:
    row = _get(signup_id)
    if row["status"] not in ("open", "closed"):
        raise HTTPException(409, "This round was already generated")
    values = _clean(body, creating=False)
    if "deadline" in values and values["deadline"] != row["deadline"]:
        values["reminded"] = 0
    if values:
        columns = ", ".join(f"{key} = ?" for key in values)
        db.execute(f"UPDATE signups SET {columns} WHERE id = ?", (*values.values(), signup_id))
    return signup_detail(signup_id, user)


@router.delete("/signups/{signup_id}")
def delete_signup(signup_id: int, _admin: sqlite3.Row = Depends(admin_user)):
    _get(signup_id)
    try:
        signups.delete(signup_id)
    except signups.SignupError as e:
        return _error(e)
    return {"ok": True}


@router.post("/signups/{signup_id}/close")
def close_signup(signup_id: int, user: sqlite3.Row = Depends(admin_user)):
    _get(signup_id)
    try:
        signups.close(signup_id, user=user)
    except signups.SignupError as e:
        return _error(e)
    return signup_detail(signup_id, user)


class Reopen(BaseModel):
    deadline: float | None = None


@router.post("/signups/{signup_id}/reopen")
def reopen_signup(signup_id: int, body: Reopen, user: sqlite3.Row = Depends(admin_user)):
    _get(signup_id)
    try:
        signups.reopen(signup_id, body.deadline)
    except signups.SignupError as e:
        return _error(e)
    return signup_detail(signup_id, user)


@router.post("/signups/{signup_id}/generate")
def generate_signup(signup_id: int, user: sqlite3.Row = Depends(admin_user)):
    _get(signup_id)
    try:
        signups.generate(signup_id, user=user)
    except signups.SignupError as e:
        return _error(e)
    return signup_detail(signup_id, user)


# ----------------------------------------------------------------------------------------------------------------------
# entries
# ----------------------------------------------------------------------------------------------------------------------
class NewEntry(BaseModel):
    config_id: int


@router.post("/signups/{signup_id}/entries")
def add_entry(signup_id: int, body: NewEntry, user: sqlite3.Row = Depends(current_user)):
    _get(signup_id)
    config = db.query_one("SELECT * FROM configs WHERE id = ?", (body.config_id,))
    # Players enter their own configs; an admin can also enter someone's config for them
    if not config or (config["user_id"] != user["id"] and not is_admin(user)):
        raise HTTPException(404, "No such config")
    try:
        result = signups.add_entry(signup_id, config, by_admin=is_admin(user))
    except signups.SignupError as e:
        return _error(e)
    return {**signup_detail(signup_id, user), "warnings": result["warnings"]}


def _entry(signup_id: int, entry_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM signup_entries WHERE id = ? AND signup_id = ?", (entry_id, signup_id))
    if not row or (row["user_id"] != user["id"] and not is_admin(user)):
        raise HTTPException(404, "No such entry")
    return row


@router.post("/signups/{signup_id}/entries/{entry_id}/refresh")
def refresh_entry(signup_id: int, entry_id: int, user: sqlite3.Row = Depends(current_user)):
    _get(signup_id)
    entry = _entry(signup_id, entry_id, user)
    try:
        result = signups.refresh_entry(signup_id, entry, by_admin=is_admin(user))
    except signups.SignupError as e:
        return _error(e)
    return {**signup_detail(signup_id, user), "warnings": result["warnings"]}


@router.delete("/signups/{signup_id}/entries/{entry_id}")
def remove_entry(signup_id: int, entry_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = _get(signup_id)
    _entry(signup_id, entry_id, user)
    # Players can withdraw while the round is open; admins can tidy up until it is generated
    allowed = signups.is_open(row) or (is_admin(user) and row["status"] in ("open", "closed"))
    if not allowed:
        raise HTTPException(409, "Sign-ups for this round are closed")
    signups.remove_entry(signup_id, entry_id)
    return signup_detail(signup_id, user)


@router.get("/signups/{signup_id}/entries/{entry_id}/download")
def download_entry(signup_id: int, entry_id: int, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    _get(signup_id)
    entry = _entry(signup_id, entry_id, user)
    path = signups.entry_path(signup_id, entry_id)
    if not path.is_file():
        raise HTTPException(410, "The file of this entry is missing from disk")
    return FileResponse(path, media_type="application/x-yaml", filename=entry["name"])
