"""Templates of generation options (presets.py): listed, saved, changed and deleted by everyone who generates."""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db, presets, settings as app_settings
from ..security import current_user, is_admin, is_organizer

router = APIRouter(prefix="/api")

_SELECT = "SELECT p.*, u.username FROM generation_presets p LEFT JOIN users u ON u.id = p.user_id"


def _generator(user: sqlite3.Row) -> sqlite3.Row:
    """Templates are for those who generate: organizers and administrators, and players where they may."""
    if not is_organizer(user) and not app_settings.get_setting("users_can_generate"):
        raise HTTPException(403, "Only administrators can generate games on this server")
    return user


def preset_json(row: sqlite3.Row, user: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "options": db.jloads(row["options"], {}), "shared": bool(row["shared"]),
            "owner": row["username"], "mine": row["user_id"] == user["id"],
            "can_edit": row["user_id"] == user["id"] or is_admin(user), "updated_at": row["updated_at"]}


def _own_or_admin(preset_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = db.query_one(f"{_SELECT} WHERE p.id = ?", (preset_id,))
    if not row or not (row["shared"] or row["user_id"] == user["id"]):
        raise HTTPException(404, "No such template")
    if row["user_id"] != user["id"] and not is_admin(user):
        raise HTTPException(403, "Only whoever saved a template, or an administrator, changes it")
    return row


@router.get("/presets")
def list_presets(user: sqlite3.Row = Depends(current_user)) -> dict:
    """Your own templates and the shared ones, by name. Organizers also get the server settings new games start with,
    to set up a game's own from."""
    _generator(user)
    rows = db.query(f"{_SELECT} WHERE p.user_id = ? OR p.shared = 1 ORDER BY p.name COLLATE NOCASE, p.id",
                    (user["id"],))
    return {"presets": [preset_json(r, user) for r in rows],
            "server_defaults": app_settings.get_setting("default_server_options") if is_organizer(user) else None}


class PresetBody(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    options: dict | None = None
    shared: bool | None = None


def _check(body: PresetBody, user: sqlite3.Row, preset_id: int | None = None) -> dict:
    values: dict = {}
    try:
        if body.name is not None:
            values["name"] = presets.clean_name(body.name)
        if body.options is not None:
            values["options"] = db.jdumps(presets.clean_options(body.options, organizer=is_organizer(user)))
    except presets.PresetError as e:
        raise HTTPException(400, str(e)) from None
    if body.shared is not None:
        if body.shared and not is_organizer(user):
            raise HTTPException(403, "Only organizers and administrators share templates")
        values["shared"] = int(body.shared)
    if "name" in values and db.query_one(
            "SELECT 1 FROM generation_presets WHERE user_id = ? AND name = ? COLLATE NOCASE AND id != ?",
            (user["id"], values["name"], preset_id or 0)):
        raise HTTPException(409, f"You already have a template called {values['name']}")
    return values


@router.post("/presets")
def create_preset(body: PresetBody, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _generator(user)
    if body.name is None or body.options is None:
        raise HTTPException(400, "A template needs a name and its options")
    values = _check(body, user)
    if db.query_one("SELECT COUNT(*) AS n FROM generation_presets WHERE user_id = ?",
                    (user["id"],))["n"] >= presets.MAX_PER_USER:
        raise HTTPException(409, f"You have {presets.MAX_PER_USER} templates already. Delete one you no longer use.")
    now = time.time()
    preset_id = db.execute("INSERT INTO generation_presets(user_id, name, options, shared, created_at, updated_at) "
                           "VALUES (?,?,?,?,?,?)",
                           (user["id"], values["name"], values["options"], values.get("shared", 0), now, now))
    audit.note(request, name=values["name"], shared=bool(values.get("shared")))
    return preset_json(db.query_one(f"{_SELECT} WHERE p.id = ?", (preset_id,)), user)


@router.put("/presets/{preset_id}")
def change_preset(preset_id: int, body: PresetBody, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _generator(user)
    row = _own_or_admin(preset_id, user)
    # an administrator changing someone else's template keeps its owner's rights: what the owner may not save stays out
    owner = db.query_one("SELECT * FROM users WHERE id = ?", (row["user_id"],)) if row["user_id"] else user
    values = _check(body, owner or user, preset_id)
    if values:
        values["updated_at"] = time.time()
        db.execute(f"UPDATE generation_presets SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                   (*values.values(), preset_id))
    audit.note(request, name=row["name"], changed=sorted(k for k in values if k != "updated_at"))
    return preset_json(db.query_one(f"{_SELECT} WHERE p.id = ?", (preset_id,)), user)


@router.delete("/presets/{preset_id}")
def delete_preset(preset_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _generator(user)
    row = _own_or_admin(preset_id, user)
    db.execute("DELETE FROM generation_presets WHERE id = ?", (preset_id,))
    audit.note(request, name=row["name"], owner=row["username"])
    return {"ok": True}
