"""Hosted games: upload, settings, start/stop, slots and the files players download."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import apbridge, backups, db, gamepaths, gamestore, history, settings as app_settings, textclient
from ..config import CONFIG
from ..rooms import ROOMS, STATUS_GOAL, RoomError, valid_password
from ..security import admin_user, current_user, is_admin
from ..textclient import TEXT_CLIENTS

router = APIRouter(prefix="/api")

# ClientStatus values from NetUtils (besides STATUS_GOAL)
STATUS_ONLINE = (5, 10, 20)
# Server options that cannot be changed on a running server (everything else is applied with /option)
RESTART_OPTIONS = {"loglevel", "auto_shutdown_minutes", "use_embedded_options"}


# ----------------------------------------------------------------------------------------------------------------------
# access rules
# ----------------------------------------------------------------------------------------------------------------------
def is_participant(game_id: int, user_id: int) -> bool:
    return db.query_one("SELECT 1 FROM game_slots WHERE game_id = ? AND user_id = ?", (game_id, user_id)) is not None


def get_game(game_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
    if not row:
        raise HTTPException(404, "No such game")
    if not (is_admin(user) or row["visibility"] == "all" or is_participant(game_id, user["id"])):
        raise HTTPException(404, "No such game")
    return row


def can_control(game: sqlite3.Row, user: sqlite3.Row) -> bool:
    return is_admin(user) or (app_settings.get_setting("users_can_start_rooms")
                              and is_participant(game["id"], user["id"]))


def file_visible(f: sqlite3.Row, game: sqlite3.Row, user: sqlite3.Row) -> bool:
    if is_admin(user):
        return True
    if f["public"]:
        return True
    if f["kind"] == "patch" and f["slot"] is not None:
        slot = db.query_one("SELECT user_id FROM game_slots WHERE game_id = ? AND slot = ?", (game["id"], f["slot"]))
        return bool(slot and slot["user_id"] == user["id"])
    return False


# ----------------------------------------------------------------------------------------------------------------------
# serialization
# ----------------------------------------------------------------------------------------------------------------------
def public_host(request: Request) -> str:
    return app_settings.get_setting("public_host") or request.url.hostname or "localhost"


def _slots_for(game_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    if not game_ids:
        return {}
    marks = ",".join("?" * len(game_ids))
    rows = db.query(f"SELECT s.*, u.username AS owner FROM game_slots s LEFT JOIN users u ON u.id = s.user_id "
                    f"WHERE s.game_id IN ({marks}) ORDER BY s.game_id, s.slot", game_ids)
    grouped: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        grouped.setdefault(r["game_id"], []).append(r)
    return grouped


def game_summary(row: sqlite3.Row, slots: list[sqlite3.Row], user: sqlite3.Row, host: str) -> dict:
    runtime = ROOMS.status(row["id"])
    mine = [s for s in slots if s["user_id"] == user["id"]]
    games = db.jloads(row["games"], [])
    players = [s for s in slots if s["type"] == 1]
    return {
        "id": row["id"], "name": row["name"], "seed_name": row["seed_name"], "source": row["source"],
        "created_at": row["created_at"], "race_mode": bool(row["race_mode"]),
        "state": runtime["state"], "error": runtime["error"], "started_at": runtime["started_at"],
        "desired_state": row["desired_state"],
        "port": row["port"], "address": f"{host}:{row['port']}" if row["port"] else None,
        "has_password": bool(row["password"]),
        "games": games, "missing_games": apbridge.missing_games(games),
        "player_count": len(players), "claimed_count": sum(1 for s in players if s["user_id"] is not None),
        "visibility": row["visibility"], "claimable": bool(row["claimable"]),
        "single_claim": bool(row["single_claim"]),
        "play_started_at": row["play_started_at"], "finished_at": row["finished_at"],
        "goal_count": sum(1 for s in players if s["goal_at"] is not None),
        "my_slots": [{"slot": s["slot"], "name": s["name"], "game": s["game"]} for s in mine],
        "my_file_count": db.query_one(
            "SELECT COUNT(*) AS n FROM game_files f JOIN game_slots s ON s.game_id = f.game_id AND s.slot = f.slot "
            "WHERE f.game_id = ? AND f.kind = 'patch' AND s.user_id = ?", (row["id"], user["id"]))["n"] if mine else 0,
        "can_control": bool(can_control(row, user)),
    }


def status_label(status: int, running: bool) -> str:
    if status == STATUS_GOAL:
        return "goal"
    return "online" if running and status in STATUS_ONLINE else "offline"


# ----------------------------------------------------------------------------------------------------------------------
# list / create
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/games")
def list_games(request: Request, user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    if is_admin(user):
        rows = db.query("SELECT * FROM games ORDER BY id DESC")
    else:
        rows = db.query("SELECT g.* FROM games g WHERE g.visibility = 'all' OR EXISTS "
                        "(SELECT 1 FROM game_slots s WHERE s.game_id = g.id AND s.user_id = ?) ORDER BY g.id DESC",
                        (user["id"],))
    slots = _slots_for([r["id"] for r in rows])
    host = public_host(request)
    return [game_summary(r, slots.get(r["id"], []), user, host) for r in rows]


@router.post("/games")
async def upload_game(request: Request, file: UploadFile = File(...), name: str = Form(""),
                      start: bool = Form(False), user: sqlite3.Row = Depends(admin_user)) -> dict:
    tmp = CONFIG.tmp_dir / f"upload-{db.now():.6f}-{user['id']}.bin"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > CONFIG.max_upload_bytes:
                    raise HTTPException(413, "File is too large")
                out.write(chunk)
        try:
            game_id = await gamestore.import_game(tmp, file.filename or "", name=name, created_by=user["id"])
        except gamestore.GameImportError as e:
            raise HTTPException(400, str(e)) from None
    finally:
        tmp.unlink(missing_ok=True)
    start_error = None
    if start:
        try:
            await ROOMS.start(game_id, announce=True)
        except RoomError as e:
            start_error = str(e)
    return {**await _detail(game_id, user, request), "start_error": start_error}


# ----------------------------------------------------------------------------------------------------------------------
# detail
# ----------------------------------------------------------------------------------------------------------------------
async def _detail(game_id: int, user: sqlite3.Row, request: Request) -> dict:
    row = get_game(game_id, user)
    admin = is_admin(user)
    slots = _slots_for([game_id]).get(game_id, [])
    summary = game_summary(row, slots, user, public_host(request))
    running = summary["state"] in ("starting", "running")
    progress = await ROOMS.progress(game_id)

    files = db.query("SELECT * FROM game_files WHERE game_id = ? ORDER BY filename COLLATE NOCASE", (game_id,))
    visible_files = [f for f in files if file_visible(f, row, user)]
    files_by_slot: dict[int, list[dict]] = {}
    for f in visible_files:
        if f["slot"] is not None and f["kind"] == "patch":
            files_by_slot.setdefault(f["slot"], []).append(_file_json(f))

    # With "one slot each", someone who already has a slot cannot claim another one.
    may_claim = bool(row["claimable"]) and not (row["single_claim"] and summary["my_slots"])
    slot_items = []
    for s in slots:
        p = progress.get(s["slot"], {})
        slot_items.append({
            "slot": s["slot"], "name": s["name"], "game": s["game"], "type": s["type"],
            "locations": s["locations"], "user_id": s["user_id"], "owner": s["owner"],
            "mine": s["user_id"] == user["id"],
            "claimable": may_claim and s["user_id"] is None and s["type"] == 1,
            "checked": p.get("checked", 0), "hints_used": p.get("hints_used", 0), "goal_at": s["goal_at"],
            "last_activity": p.get("last_activity"),
            "status": status_label(p.get("status", 0), running) if s["type"] == 1 else None,
            "files": files_by_slot.get(s["slot"], []),
        })
    shared = [_file_json(f) for f in visible_files if not (f["slot"] is not None and f["kind"] == "patch")]
    total = sum(s["locations"] for s in slots if s["type"] == 1)
    checked = sum(i["checked"] for i in slot_items if i["type"] == 1)
    # The generation job knows the configs and options of the game, to generate it again (jobs are pruned eventually)
    job = db.query_one("SELECT created_by FROM jobs WHERE id = ?", (row["job_id"],)) if row["job_id"] else None

    detail = {
        **summary,
        "slots": slot_items, "shared_files": shared,
        "progress": {"checked": checked, "total": total},
        "password": row["password"] if (admin or summary["my_slots"]) else None,
        "options": db.jloads(row["options"], {}),
        "generator_version": row["generator_version"], "min_server_version": row["min_server_version"],
        "notes": row["notes"], "job_id": row["job_id"],
        "job_available": bool(job and (admin or job["created_by"] == user["id"])),
        "share_spoiler_when_done": bool(row["share_spoiler_when_done"]),
        "ap_version": apbridge.ap_version(),
    }
    if admin:
        detail["server_password"] = row["server_password"]
        detail["has_save"] = gamepaths.save_path(game_id).is_file()
    return detail


def _file_json(f: sqlite3.Row) -> dict:
    return {"id": f["id"], "filename": f["filename"], "size": f["size"], "kind": f["kind"], "slot": f["slot"],
            "public": bool(f["public"])}


@router.get("/games/{game_id}")
async def game_detail(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------------------------------------------------------
class GamePatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    port: int | None = None
    password: str | None = Field(default=None, max_length=64)
    server_password: str | None = Field(default=None, max_length=64)
    options: dict | None = None
    visibility: str | None = None
    claimable: bool | None = None
    single_claim: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)
    share_spoiler_when_done: bool | None = None
    finished: bool | None = None


@router.patch("/games/{game_id}")
async def patch_game(game_id: int, body: GamePatch, request: Request, user: sqlite3.Row = Depends(admin_user)) -> dict:
    row = get_game(game_id, user)
    changes: dict[str, object] = {}
    given = body.model_fields_set
    restart_required = False

    if "name" in given and body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "The name cannot be empty")
        changes["name"] = body.name.strip()
    if "port" in given and body.port is not None:
        if not (1024 <= body.port <= 65535):
            raise HTTPException(400, "Port must be between 1024 and 65535")
        if db.query_one("SELECT 1 FROM games WHERE port = ? AND id != ?", (body.port, game_id)):
            raise HTTPException(409, f"Port {body.port} is already used by another game")
        if body.port != row["port"]:
            changes["port"] = body.port
            restart_required = ROOMS.is_active(game_id)
    for field in ("password", "server_password"):
        if field in given and getattr(body, field) is not None:
            value = getattr(body, field)
            if value and not valid_password(value):
                raise HTTPException(400, "Passwords cannot contain spaces or control characters, "
                                         "and 'null' and 'none' are reserved by the server")
            changes[field] = value or None
    if "options" in given and body.options is not None:
        current = app_settings.clean_server_options(db.jloads(row["options"], {}))
        try:
            merged = app_settings.clean_server_options(body.options, base=current)
        except app_settings.SettingsError as e:
            raise HTTPException(400, str(e)) from None
        changes["options"] = db.jdumps(merged)
        if any(merged[key] != current[key] for key in RESTART_OPTIONS):
            restart_required = restart_required or ROOMS.is_active(game_id)
    if "visibility" in given and body.visibility is not None:
        if body.visibility not in ("participants", "all"):
            raise HTTPException(400, "Visibility must be 'participants' or 'all'")
        changes["visibility"] = body.visibility
    if "claimable" in given and body.claimable is not None:
        changes["claimable"] = int(body.claimable)
    if "single_claim" in given and body.single_claim is not None:
        changes["single_claim"] = int(body.single_claim)
    if "notes" in given:
        changes["notes"] = (body.notes or "").strip() or None
    if "share_spoiler_when_done" in given and body.share_spoiler_when_done is not None:
        changes["share_spoiler_when_done"] = int(body.share_spoiler_when_done)

    if changes:
        assignments = ", ".join(f"{k} = ?" for k in changes)
        db.execute(f"UPDATE games SET {assignments} WHERE id = ?", [*changes.values(), game_id])
        await ROOMS.apply_options(game_id)
    if "finished" in given and body.finished is not None:  # after the spoiler setting, which finishing applies
        history.finish(game_id, body.finished)
    return {**await _detail(game_id, user, request), "restart_required": restart_required}


@router.delete("/games/{game_id}")
async def remove_game(game_id: int, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    await gamestore.delete_game(game_id)
    return {"ok": True}


# ----------------------------------------------------------------------------------------------------------------------
# running
# ----------------------------------------------------------------------------------------------------------------------
def _controllable(game_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = get_game(game_id, user)
    if not can_control(row, user):
        raise HTTPException(403, "You are not allowed to control this game")
    return row


@router.post("/games/{game_id}/start")
async def start_game(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _controllable(game_id, user)
    try:
        await ROOMS.start(game_id, announce=True)
    except RoomError as e:
        raise HTTPException(409, str(e)) from None
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/stop")
async def stop_game(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _controllable(game_id, user)
    await ROOMS.stop(game_id)
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/restart")
async def restart_game(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _controllable(game_id, user)
    try:
        await ROOMS.restart(game_id)
    except RoomError as e:
        raise HTTPException(409, str(e)) from None
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/reset-save")
async def reset_save(game_id: int, request: Request, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    if ROOMS.is_active(game_id):
        raise HTTPException(409, "Stop the server before resetting its progress")
    backups.create(game_id, "before-reset")  # so a reset can be undone
    gamepaths.save_path(game_id).unlink(missing_ok=True)
    ROOMS.forget_progress(game_id)
    history.forget(game_id)
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# backups of the save
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/games/{game_id}/backups")
def list_backups(game_id: int, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    return {"backups": backups.listing(game_id), "has_save": gamepaths.save_path(game_id).is_file(),
            "interval_minutes": backups.AUTO_INTERVAL // 60, "keep_auto": backups.KEEP_AUTO}


@router.post("/games/{game_id}/backups")
def create_backup(game_id: int, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    entry = backups.create(game_id, "manual")
    if entry is None:
        raise HTTPException(409, "This game has no saved progress yet")
    return list_backups(game_id, user)


@router.post("/games/{game_id}/backups/{name}/restore")
async def restore_backup(game_id: int, name: str, request: Request, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    if ROOMS.is_active(game_id):
        raise HTTPException(409, "Stop the server before restoring a backup")
    try:
        backups.restore(game_id, name)
    except backups.BackupError as e:
        raise HTTPException(404, str(e)) from None
    ROOMS.forget_progress(game_id)
    return await _detail(game_id, user, request)


@router.get("/games/{game_id}/backups/{name}")
def download_backup(game_id: int, name: str, user: sqlite3.Row = Depends(admin_user)) -> FileResponse:
    get_game(game_id, user)
    try:
        path = backups.path_of(game_id, name)
    except backups.BackupError as e:
        raise HTTPException(404, str(e)) from None
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.delete("/games/{game_id}/backups/{name}")
def delete_backup(game_id: int, name: str, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    try:
        backups.delete(game_id, name)
    except backups.BackupError as e:
        raise HTTPException(404, str(e)) from None
    return list_backups(game_id, user)


class Command(BaseModel):
    command: str = Field(max_length=500)


@router.post("/games/{game_id}/command")
async def send_command(game_id: int, body: Command, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    try:
        await ROOMS.send_command(game_id, body.command)
    except RoomError as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True}


@router.get("/games/{game_id}/log")
def game_log(game_id: int, after: int = 0, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    return ROOMS.log_since(game_id, after)


# ----------------------------------------------------------------------------------------------------------------------
# slots
# ----------------------------------------------------------------------------------------------------------------------
class SlotAssign(BaseModel):
    user_id: int | None


def _get_slot(game_id: int, slot: int) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM game_slots WHERE game_id = ? AND slot = ?", (game_id, slot))
    if not row:
        raise HTTPException(404, "No such slot")
    return row


@router.get("/games/{game_id}/history")
def progress_history(game_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Every player's checked locations over time, when they reached their goal, and when the game was played."""
    get_game(game_id, user)
    return history.series(game_id)


@router.get("/games/{game_id}/slots/{slot}/tracker")
async def slot_tracker(game_id: int, slot: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """What a slot has received, which of its locations are checked, and its hints. For its owner and admins."""
    get_game(game_id, user)
    row = db.query_one("SELECT user_id, type FROM game_slots WHERE game_id = ? AND slot = ?", (game_id, slot))
    if not row or row["type"] != 1 or (row["user_id"] != user["id"] and not is_admin(user)):
        raise HTTPException(404, "No such slot")
    return await ROOMS.tracker(game_id, slot)


# ----------------------------------------------------------------------------------------------------------------------
# text client: the manager connects to the server as the player (textclient.py)
# ----------------------------------------------------------------------------------------------------------------------
def _own_slot(game_id: int, slot: int, user: sqlite3.Row) -> None:
    """Only the player of a slot talks in its name; admins have the console."""
    get_game(game_id, user)
    row = db.query_one("SELECT user_id, type FROM game_slots WHERE game_id = ? AND slot = ?", (game_id, slot))
    if not row or row["type"] != 1 or row["user_id"] != user["id"]:
        raise HTTPException(404, "No such slot")


class ClientPoll(BaseModel):
    after: int = 0


@router.post("/games/{game_id}/slots/{slot}/client")
async def text_client(game_id: int, slot: int, body: ClientPoll, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Keeps the connection open (opening it if needed) and returns the messages after `after`."""
    _own_slot(game_id, slot, user)
    session = TEXT_CLIENTS.session(game_id, slot, user["id"])
    after = body.after if body.after <= session.seq else 0  # a new connection numbers its messages from 1 again
    connected = session.state == "connected"
    return {"state": session.state, "error": session.error, "last": session.seq,
            "messages": [m for m in session.messages if m["seq"] > after],
            "hint_points": session.hint_points if connected else None,
            "hint_cost": session.hint_cost if connected else None}


class ClientSay(BaseModel):
    text: str = Field(max_length=textclient.MAX_MESSAGE_LENGTH * 2)


@router.post("/games/{game_id}/slots/{slot}/client/say")
async def text_client_say(game_id: int, slot: int, body: ClientSay, user: sqlite3.Row = Depends(current_user)) -> dict:
    _own_slot(game_id, slot, user)
    try:
        await TEXT_CLIENTS.say(TEXT_CLIENTS.session(game_id, slot, user["id"]), body.text)
    except textclient.TextClientError as e:
        raise HTTPException(e.status, e.message) from None
    return {"ok": True}


@router.get("/games/{game_id}/slots/{slot}/client/names")
async def text_client_names(game_id: int, slot: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """The slot's items and locations, to suggest them after !hint and !hint_location."""
    _own_slot(game_id, slot, user)
    session = TEXT_CLIENTS.sessions.get((game_id, slot, user["id"]))
    return textclient.suggestions(session) if session else {"items": [], "locations": []}


@router.delete("/games/{game_id}/slots/{slot}/client")
async def text_client_close(game_id: int, slot: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    _own_slot(game_id, slot, user)
    await TEXT_CLIENTS.close((game_id, slot, user["id"]))
    return {"ok": True}


@router.post("/games/{game_id}/slots/{slot}/assign")
async def assign_slot(game_id: int, slot: int, body: SlotAssign, request: Request,
                      user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if target["type"] != 1:
        raise HTTPException(400, "Only player slots can be assigned")
    if body.user_id is not None and not db.query_one("SELECT 1 FROM users WHERE id = ?", (body.user_id,)):
        raise HTTPException(404, "No such user")
    db.execute("UPDATE game_slots SET user_id = ? WHERE game_id = ? AND slot = ?", (body.user_id, game_id, slot))
    added = 0
    if body.user_id is not None:
        added = await asyncio.to_thread(gamestore.sync_game_yamls, game_id, slot)
    return {**await _detail(game_id, user, request), "configs_added": added}


@router.post("/games/{game_id}/slots/{slot}/claim")
async def claim_slot(game_id: int, slot: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if not row["claimable"] or target["type"] != 1:
        raise HTTPException(403, "Slots of this game cannot be claimed")
    with db.connection() as conn:  # one locked transaction: two quick clicks cannot both pass the check
        if row["single_claim"] and conn.execute("SELECT 1 FROM game_slots WHERE game_id = ? AND user_id = ?",
                                                (game_id, user["id"])).fetchone():
            raise HTTPException(409, "You already have a slot in this game. Each player may claim only one.")
        claimed = conn.execute("UPDATE game_slots SET user_id = ? WHERE game_id = ? AND slot = ? AND user_id IS NULL",
                               (user["id"], game_id, slot)).rowcount
    if not claimed:
        raise HTTPException(409, "Somebody else already claimed that slot")
    added = await asyncio.to_thread(gamestore.sync_game_yamls, game_id, slot)
    return {**await _detail(game_id, user, request), "configs_added": added}


@router.post("/games/{game_id}/slots/{slot}/unclaim")
async def unclaim_slot(game_id: int, slot: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if target["user_id"] != user["id"]:
        raise HTTPException(403, "That slot is not yours")
    db.execute("UPDATE game_slots SET user_id = NULL WHERE game_id = ? AND slot = ?", (game_id, slot))
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# files
# ----------------------------------------------------------------------------------------------------------------------
def _get_file(game_id: int, file_id: int) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM game_files WHERE id = ? AND game_id = ?", (file_id, game_id))
    if not row:
        raise HTTPException(404, "No such file")
    return row


@router.get("/games/{game_id}/files/{file_id}")
def download_file(game_id: int, file_id: int, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    game = get_game(game_id, user)
    f = _get_file(game_id, file_id)
    if not file_visible(f, game, user):
        raise HTTPException(404, "No such file")
    path = gamepaths.files_dir(game_id) / f["filename"]
    if not path.is_file():
        raise HTTPException(410, "The file is missing from disk")
    return FileResponse(path, filename=f["filename"], media_type="application/octet-stream")


@router.get("/games/{game_id}/my-files.zip")
def download_my_files(game_id: int, background: BackgroundTasks,
                      user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    game = get_game(game_id, user)
    rows = db.query("SELECT f.* FROM game_files f JOIN game_slots s ON s.game_id = f.game_id AND s.slot = f.slot "
                    "WHERE f.game_id = ? AND f.kind = 'patch' AND s.user_id = ?", (game_id, user["id"]))
    rows = [r for r in rows if (gamepaths.files_dir(game_id) / r["filename"]).is_file()]
    if not rows:
        raise HTTPException(404, "You have no files in this game")
    fd, name = tempfile.mkstemp(suffix=".zip", dir=CONFIG.tmp_dir)
    os.close(fd)  # ZipFile opens it again by name; an open descriptor would keep the file's space until restart
    tmp = Path(name)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for r in rows:
            zf.write(gamepaths.files_dir(game_id) / r["filename"], arcname=r["filename"])
    background.add_task(tmp.unlink, missing_ok=True)
    return FileResponse(tmp, media_type="application/zip", filename=f"{game['name']} - my files.zip".replace("/", "_"))


@router.get("/games/{game_id}/multidata")
def download_multidata(game_id: int, user: sqlite3.Row = Depends(admin_user)) -> FileResponse:
    game = get_game(game_id, user)
    path = gamepaths.multidata_path(game_id)
    if not path.is_file():
        raise HTTPException(410, "The file is missing from disk")
    return FileResponse(path, filename=f"AP_{game['seed_name'] or game_id}.archipelago",
                        media_type="application/octet-stream")


@router.post("/games/{game_id}/files")
async def upload_file(game_id: int, request: Request, file: UploadFile = File(...), slot: int | None = Form(None),
                      public: bool = Form(False), user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    if slot is not None:
        _get_slot(game_id, slot)
    tmp = CONFIG.tmp_dir / f"upload-{db.now():.6f}-{user['id']}.bin"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > CONFIG.max_upload_bytes:
                    raise HTTPException(413, "File is too large")
                out.write(chunk)
        try:
            gamestore.add_file(game_id, file.filename or "file", tmp, slot=slot, public=public)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A file with that name already exists") from None
    finally:
        tmp.unlink(missing_ok=True)
    return await _detail(game_id, user, request)


class FilePatch(BaseModel):
    slot: int | None = None
    public: bool | None = None


@router.patch("/games/{game_id}/files/{file_id}")
async def patch_file(game_id: int, file_id: int, body: FilePatch, request: Request,
                     user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    _get_file(game_id, file_id)
    if "slot" in body.model_fields_set:
        if body.slot is not None:
            _get_slot(game_id, body.slot)
        db.execute("UPDATE game_files SET slot = ? WHERE id = ?", (body.slot, file_id))
    if body.public is not None:
        db.execute("UPDATE game_files SET public = ? WHERE id = ?", (int(body.public), file_id))
    return await _detail(game_id, user, request)


@router.delete("/games/{game_id}/files/{file_id}")
async def delete_file(game_id: int, file_id: int, request: Request, user: sqlite3.Row = Depends(admin_user)) -> dict:
    get_game(game_id, user)
    f = _get_file(game_id, file_id)
    db.execute("DELETE FROM game_files WHERE id = ?", (file_id,))
    (gamepaths.files_dir(game_id) / f["filename"]).unlink(missing_ok=True)
    return await _detail(game_id, user, request)
